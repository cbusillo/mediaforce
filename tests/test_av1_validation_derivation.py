from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from typing import Literal, Sequence
import unittest
from unittest.mock import patch

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.tuning.av1_validation_derivation import (
    AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY,
    AV1_VALIDATION_DERIVATION_REVIEW_LANES,
    AV1_VALIDATION_DERIVATION_PERSONALIZATION_EXCLUSION_REASON,
    AV1ValidationDerivationAttempt,
    AV1ValidationDerivationCandidateProposal,
    AV1ValidationDerivationError,
    AV1ValidationDerivationPlan,
    AV1ValidationDerivationReviewClaim,
    AV1ValidationDerivationTerminalRecord,
    _attempt_semantic_payload,
    _derivation_id,
    _payload_sha256,
    _terminal_semantic_payload,
    assert_av1_validation_derivation_authorization_active,
    av1_validation_derivation_plan_public_summary,
    av1_validation_derivation_statistics_contract_sha256,
    build_av1_validation_derivation_attempt,
    build_av1_validation_derivation_plan,
    build_av1_validation_derivation_review_claim,
    build_av1_validation_derivation_review_attestation,
    build_av1_validation_derivation_review_envelope,
    build_av1_validation_derivation_terminal_record,
    evaluate_av1_validation_derivation_candidate,
    _finalize_and_write_av1_validation_derivation_candidate_lock as finalize_and_write_av1_validation_derivation_candidate_lock,
    finalize_av1_validation_derivation_candidate_lock,
    load_av1_validation_derivation_candidate_proposal,
    _load_verified_av1_validation_derivation_candidate_lock as load_verified_av1_validation_derivation_candidate_lock,
    load_av1_validation_derivation_plan,
    load_av1_validation_derivation_attempts,
    load_av1_validation_derivation_review_claims,
    load_av1_validation_derivation_review_envelopes,
    load_av1_validation_derivation_terminal_records,
    resolve_av1_validation_derivation_verdict_intent,
    validate_av1_validation_derivation_review_run_evidence,
    validate_av1_validation_derivation_plan_binding,
    write_av1_validation_derivation_candidate_proposal,
    write_av1_validation_derivation_plan,
    write_av1_validation_derivation_review_claim,
    write_av1_validation_derivation_review_envelope,
    write_av1_validation_derivation_attempt,
    write_av1_validation_derivation_assignment_claim,
    write_av1_validation_derivation_terminal_record,
)
from mediaforce.web.runtime.av1_validation_derivation import (
    _assert_next_assignment,
    _current_derivation_review_artifact_fingerprint,
    _prepare_derivation_review_root,
    _recover_interrupted_derivation_state,
    _secure_derivation_review_media,
    assert_av1_validation_derivation_execution_contract,
    av1_validation_derivation_execution_environment_sha256,
    av1_validation_derivation_runtime_context_sha256,
    finalize_av1_validation_derivation_candidate_lock as finalize_runtime_av1_validation_derivation_candidate_lock,
    load_verified_av1_validation_derivation_candidate_lock as load_verified_runtime_av1_validation_derivation_candidate_lock,
    record_av1_validation_derivation_visual_verdict,
    run_av1_validation_derivation_assignment,
)
from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1ColdStartValidationError,
    build_av1_cold_start_validation_candidate_lock,
)
from mediaforce.tuning.av1_validation_partition import (
    AV1ValidationPartitionAssignment,
    AV1ValidationPartitionExpectations,
    AV1ValidationPartitionSource,
    AV1ValidationPrivatePartition,
    av1_validation_partition_key_id,
    build_av1_validation_private_partition,
)
from mediaforce.tuning.av1_validation_v2 import (
    build_av1_validation_v2_derivation_authorization,
    load_av1_validation_manifest_v2,
)
from mediaforce.tuning.content_intent_observations import (
    ContentIntentBoundaryCompatibilityV1,
    ContentIntentBoundaryObservation,
    ContentIntentObservationConflictError,
    ContentIntentObservationBuildResult,
    _build_observation,
    _rehash_observation,
    build_content_intent_boundary_compatibility,
    correct_content_intent_boundary_observation,
    replay_content_intent_personalization,
    withdraw_content_intent_boundary_observation,
)
from scripts import verify_av1_cold_start_preregistration


V2_MANIFEST_PATH = Path("docs/validation/av1-cold-start-preregistration-v2.json")
SELECTED_AT = "2026-07-27T22:50:00Z"
AUTHORIZED_AT = "2026-07-28T00:00:00Z"
VALID_UNTIL = "2026-08-01T00:00:00Z"
REVIEW_RUNNER_BYTES = b"\xcf\xfa\xed\xfe" + b"test-code-binary"


class AV1ValidationDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_directory = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_directory.cleanup)
        runtime_root = Path(runtime_directory.name)
        self.runtime_config = SimpleNamespace(
            paths=SimpleNamespace(
                db_path=runtime_root / "mediaforce.sqlite3",
                review_dir=runtime_root / "review",
                web_state_dir=runtime_root / "state",
            )
        )
        toolchain_patcher = patch(
            "mediaforce.web.runtime.av1_validation_derivation.quality_toolchain_identity",
            return_value={
                "schema_version": 1,
                "status": "available",
                "encoder": "libsvtav1",
                "encoder_version": "SVT-AV1 Encoder Lib vtest",
                "encoder_runtime_version": "ffmpeg version test",
                "encoder_runtime_signature_id": "erti1_test_contract",
                "quality_tool": "ab-av1",
                "quality_tool_version": "ab-av1 test",
                "metric_runtime_signature_id": "qmri1_test_contract",
                "signature_id": "qti1_test_contract",
            },
        )
        toolchain_patcher.start()
        self.addCleanup(toolchain_patcher.stop)
        self.manifest = load_av1_validation_manifest_v2(V2_MANIFEST_PATH)
        self.expectations = AV1ValidationPartitionExpectations(
            compatibility_signature="av1vcompat1_test_contract",
            base_policy_signature="av1vbasepolicy1_test_contract",
            quality_metric="vmaf",
            quality_target=85.0,
            minimum_quality_score=80.0,
        )
        self.sources = _partition_sources(self.expectations)
        self.token_key = b"k" * 32
        self.partition = build_av1_validation_private_partition(
            manifest=self.manifest,
            eligibility_attestation_id=self.manifest.eligibility_attestation_id,
            eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
            sources=self.sources,
            expectations=self.expectations,
            token_key=self.token_key,
            expected_token_key_id=av1_validation_partition_key_id(self.token_key),
            selected_at=SELECTED_AT,
        )
        runtime_context_sha256 = av1_validation_derivation_runtime_context_sha256(
            self.runtime_config
        )
        execution_environment_sha256 = (
            av1_validation_derivation_execution_environment_sha256(
                quality_metric=self.expectations.quality_metric,
            )
        )
        self.authorization = build_av1_validation_v2_derivation_authorization(
            manifest=self.manifest,
            selection_lock_sha256=self.partition.selection_lock_sha256,
            derivation_partition_sha256=self.partition.derivation_partition_sha256,
            runtime_context_sha256=runtime_context_sha256,
            execution_environment_sha256=execution_environment_sha256,
            statistics_contract_sha256=(
                av1_validation_derivation_statistics_contract_sha256(self.manifest)
            ),
            review_runner_canonical_path_sha256=f"sha256:{'a' * 64}",
            review_runner_binary_sha256=(
                f"sha256:{hashlib.sha256(REVIEW_RUNNER_BYTES).hexdigest()}"
            ),
            authorized_at=AUTHORIZED_AT,
            valid_until=VALID_UNTIL,
        )
        self.plan = build_av1_validation_derivation_plan(
            manifest=self.manifest,
            partition=self.partition,
            authorization=self.authorization,
            runtime_context_sha256=runtime_context_sha256,
        )
        self.runtime_artifact_root = (
            self.runtime_config.paths.web_state_dir
            / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
            / self.plan.partition_id
        )
        write_av1_validation_derivation_plan(
            self.runtime_artifact_root,
            self.plan,
        )

    def test_plan_contains_only_exact_reserved_derivation_assignments(self) -> None:
        self.assertEqual(len(self.plan.assignments), 24)
        self.assertEqual({assignment.role for assignment in self.plan.assignments}, {"derivation"})
        self.assertEqual(
            sorted(
                sum(assignment.cell_plan_id == cell_plan_id for assignment in self.plan.assignments)
                for cell_plan_id in {assignment.cell_plan_id for assignment in self.plan.assignments}
            ),
            [12, 12],
        )
        summary = av1_validation_derivation_plan_public_summary(self.plan)
        serialized = json.dumps(summary)
        self.assertNotIn("local_item_id", serialized)
        self.assertNotIn("source_token", serialized)
        self.assertTrue(summary["derivation_execution_authorized"])
        self.assertFalse(summary["holdout_execution_authorized"])
        self.assertFalse(summary["guided_probe_allowed"])

    def test_plan_binding_rejects_digest_valid_assignment_drift(self) -> None:
        assignments = list(self.plan.assignments)
        assignments[0] = replace(
            assignments[0],
            local_item_id=assignments[1].local_item_id,
        )
        semantic_payload = self.plan.semantic_payload()
        semantic_payload["assignments"] = [
            assignment.to_payload() for assignment in assignments
        ]
        plan_id = _derivation_id("plan", semantic_payload)
        drifted_plan = AV1ValidationDerivationPlan(
            plan_id=plan_id,
            manifest_id=self.plan.manifest_id,
            manifest_payload_sha256=self.plan.manifest_payload_sha256,
            partition_id=self.plan.partition_id,
            partition_payload_sha256=self.plan.partition_payload_sha256,
            selection_lock_sha256=self.plan.selection_lock_sha256,
            derivation_partition_sha256=self.plan.derivation_partition_sha256,
            runtime_context_sha256=self.plan.runtime_context_sha256,
            authorization=self.plan.authorization,
            assignments=tuple(assignments),
            payload_sha256=_payload_sha256({
                "plan_id": plan_id,
                **semantic_payload,
            }),
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "does not match the private partition",
        ):
            validate_av1_validation_derivation_plan_binding(
                plan=drifted_plan,
                partition=self.partition,
            )

    def test_derivation_authorization_must_be_active_before_execution(self) -> None:
        assert_av1_validation_derivation_authorization_active(
            self.plan,
            at=AUTHORIZED_AT,
        )
        for timestamp in ("2026-07-27T23:59:59Z", VALID_UNTIL):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "outside its authorization window",
            ):
                assert_av1_validation_derivation_authorization_active(
                    self.plan,
                    at=timestamp,
                )

    def test_owner_only_plan_and_terminal_files_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private"
            root.mkdir(mode=0o700)
            artifact_root = (
                root
                / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
                / self.plan.partition_id
            )
            plan_path = write_av1_validation_derivation_plan(
                artifact_root,
                self.plan,
            )
            self.assertEqual(os.stat(plan_path).st_mode & 0o777, 0o600)
            self.assertEqual(load_av1_validation_derivation_plan(plan_path), self.plan)
            with self.assertRaisesRegex(AV1ValidationDerivationError, "already exists"):
                write_av1_validation_derivation_plan(artifact_root, self.plan)

            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "partition-global canonical root",
            ):
                write_av1_validation_derivation_plan(
                    root / "alternate" / self.plan.partition_id,
                    self.plan,
                )

            record = self._failed_record(self.plan.assignments[0].assignment_id)
            attempt = self._failed_attempt(self.plan.assignments[0].assignment_id)
            attempts_dir = root / "attempts"
            write_av1_validation_derivation_attempt(attempts_dir, attempt)
            self.assertEqual(load_av1_validation_derivation_attempts(attempts_dir), (attempt,))
            with self.assertRaisesRegex(AV1ValidationDerivationError, "already exists"):
                write_av1_validation_derivation_attempt(attempts_dir, attempt)

            records_dir = root / "records"
            write_av1_validation_derivation_terminal_record(records_dir, record)
            self.assertEqual(load_av1_validation_derivation_terminal_records(records_dir), (record,))
            with self.assertRaisesRegex(AV1ValidationDerivationError, "already exists"):
                write_av1_validation_derivation_terminal_record(records_dir, record)

    def test_assignment_and_review_claim_directories_are_fsynced_through_parents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts_directory = root / "nested" / "claims" / "attempts"
            with patch(
                "mediaforce.tuning.av1_validation_derivation._fsync_directory"
            ) as fsync_directory:
                write_av1_validation_derivation_assignment_claim(
                    attempts_directory,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    plan_id=self.plan.plan_id,
                    authorization_id=self.plan.authorization.authorization_id,
                    claimed_at="2026-07-28T01:00:00Z",
                )
            fsynced_paths = {
                call.args[0] for call in fsync_directory.call_args_list
            }
            self.assertTrue({
                root,
                root / "nested",
                root / "nested" / "claims",
                attempts_directory,
            }.issubset(fsynced_paths))

        proposal = self._candidate_proposal()
        claim = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            lane="architecture",
            review_run_id="70000000-0000-0000-0000-000000000001",
            review_runner_canonical_path_sha256=(
                self.authorization.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=(
                self.authorization.review_runner_binary_sha256
            ),
            claimed_at="2026-07-28T03:00:01Z",
        )
        with patch(
            "mediaforce.tuning.av1_validation_derivation._fsync_directory"
        ) as fsync_directory:
            write_av1_validation_derivation_review_claim(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
            )
        fsynced_paths = {
            call.args[0] for call in fsync_directory.call_args_list
        }
        resolved_artifact_root = self.runtime_artifact_root.resolve()
        self.assertTrue({
            resolved_artifact_root,
            resolved_artifact_root / "review-claims",
            (
                resolved_artifact_root
                / "review-claims"
                / proposal.proposal_id
            ),
        }.issubset(fsynced_paths))

    def test_review_attestation_identity_comes_from_code_managed_result(self) -> None:
        agent_id = "12345678-1234-1234-1234-123456789abc"
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        assignments = [
            assignment
            for assignment in self.plan.assignments
            if assignment.cell_plan_id == cell_plan_id
        ]
        records = [
            self._observed_record(assignment.assignment_id, crf=28.0)
            for assignment in assignments
        ]
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=self._current_observations(records),
            proposed_at="2026-07-28T02:00:00Z",
        )
        assert evaluation.proposal is not None
        proposal = evaluation.proposal
        expected_claim = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            lane="architecture",
            review_run_id=agent_id,
            review_runner_canonical_path_sha256=(
                self.authorization.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=(
                self.authorization.review_runner_binary_sha256
            ),
            claimed_at="2026-07-28T03:00:00Z",
        )
        marker = {
            "decision": "approved",
            "lane": "architecture",
            "proposal_id": proposal.proposal_id,
            "proposal_payload_sha256": proposal.payload_sha256,
            "review_claim_id": expected_claim.claim_id,
            "review_claim_payload_sha256": expected_claim.payload_sha256,
            "review_run_id": agent_id,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code_binary = root / "code"
            code_binary.write_bytes(REVIEW_RUNNER_BYTES)
            code_binary.chmod(0o700)
            final_message = (
                "Review complete.\n"
                "MEDIAFORCE_AV1_REVIEW_V2 "
                f"{json.dumps(marker, sort_keys=True, separators=(',', ':'))}"
            )
            prompt = verify_av1_cold_start_preregistration._agent_review_prompt(
                proposal=proposal,
                claim=expected_claim,
            )
            stdout = "\n".join((
                json.dumps({
                    "provider": "test",
                    "model": "test-model",
                    "workdir": str(root),
                    "approval": "never",
                    "sandbox": "read-only",
                }),
                json.dumps({"prompt": prompt}),
                json.dumps({
                    "msg": {
                        "type": "agent_message",
                        "message": final_message,
                    }
                }),
                json.dumps({
                    "msg": {
                        "type": "task_lifecycle",
                        "phase": "quiescent",
                        "last_agent_message": final_message,
                    }
                }),
            ))
            completed = SimpleNamespace(
                returncode=0,
                stdout=stdout,
                stderr="",
            )
            with (
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_authorized_review_runner_identity",
                    return_value=(
                        code_binary,
                        self.authorization.review_runner_canonical_path_sha256,
                        self.authorization.review_runner_binary_sha256,
                        REVIEW_RUNNER_BYTES,
                    ),
                ),
                patch.object(
                    verify_av1_cold_start_preregistration.subprocess,
                    "run",
                    return_value=completed,
                ) as run_review,
                patch.object(
                    verify_av1_cold_start_preregistration.uuid,
                    "uuid4",
                    return_value=agent_id,
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_now_iso",
                    return_value="2026-07-28T03:00:00Z",
                ),
            ):
                claim, evidence, decision = (
                    verify_av1_cold_start_preregistration._run_code_agent_review(
                        artifact_root=self.runtime_artifact_root,
                        plan=self.plan,
                        proposal=proposal,
                        lane="architecture",
                    )
                )
            self.assertEqual(claim, expected_claim)
            self.assertEqual(decision, "approved")
            launched_runner = Path(run_review.call_args.args[0][0])
            self.assertNotEqual(launched_runner, code_binary)
            self.assertFalse(launched_runner.exists())
            review_command = run_review.call_args.args[0]
            self.assertIn('shell_environment_policy.inherit="none"', review_command)
            review_environment = run_review.call_args.kwargs["env"]
            self.assertEqual(
                review_environment["PATH"],
                verify_av1_cold_start_preregistration._AGENT_REVIEW_SAFE_PATH,
            )
            evidence_payload = json.loads(evidence)
            self.assertEqual(evidence_payload["review_run_id"], agent_id)
            self.assertEqual(evidence_payload["returncode"], 0)
            self.assertNotIn(str(code_binary), evidence.decode("utf-8"))
            review = build_av1_validation_derivation_review_attestation(
                proposal=proposal,
                claim=claim,
                review_evidence_sha256=(
                    f"sha256:{hashlib.sha256(evidence).hexdigest()}"
                ),
                decision=decision,
                reviewed_at="2026-07-28T03:00:00Z",
            )
            validate_av1_validation_derivation_review_run_evidence(
                evidence,
                review=review,
            )

    def test_review_runner_rejects_interpreter_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = Path(directory) / "code"
            runner.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            runner.chmod(0o700)
            with (
                patch.object(
                    verify_av1_cold_start_preregistration.shutil,
                    "which",
                    return_value=str(runner),
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_trusted_code_ancestor_path",
                    return_value=runner.resolve(),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "native Mach-O",
                ),
            ):
                verify_av1_cold_start_preregistration._review_runner_identity()

    def test_review_runner_environment_removes_injection_overrides(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DYLD_INSERT_LIBRARIES": "/private/injected.dylib",
                "HOME": "/private/fake-home",
                "GIT_EXEC_PATH": "/private/git-tools",
                "LD_PRELOAD": "/private/injected.so",
                "NODE_OPTIONS": "--require=/private/injected.js",
                "OPENAI_BASE_URL": "https://invalid.example",
                "PATH": "/private/bin:/usr/bin",
                "SAFE_REVIEW_VALUE": "retained",
            },
            clear=False,
        ):
            environment = (
                verify_av1_cold_start_preregistration._review_runner_environment()
            )
        self.assertEqual(
            environment["PATH"],
            verify_av1_cold_start_preregistration._AGENT_REVIEW_SAFE_PATH,
        )
        self.assertNotIn("SAFE_REVIEW_VALUE", environment)
        self.assertEqual(
            environment["HOME"],
            verify_av1_cold_start_preregistration._review_user_home(),
        )
        self.assertEqual(environment["SHELL"], "/bin/zsh")
        for key in (
            "AWS_SECRET_ACCESS_KEY",
            "GITHUB_TOKEN",
            "DYLD_INSERT_LIBRARIES",
            "GIT_EXEC_PATH",
            "LD_PRELOAD",
            "NODE_OPTIONS",
            "OPENAI_BASE_URL",
        ):
            self.assertNotIn(key, environment)

    def test_review_runner_rejects_non_ancestor_native_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = Path(directory) / "code"
            runner.write_bytes(REVIEW_RUNNER_BYTES)
            runner.chmod(0o700)
            with (
                patch.object(
                    verify_av1_cold_start_preregistration.shutil,
                    "which",
                    return_value=str(runner),
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_trusted_code_ancestor_path",
                    return_value=Path("/private/trusted/code"),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "active trusted runner",
                ),
            ):
                verify_av1_cold_start_preregistration._review_runner_identity()

    def test_review_runner_rejects_path_substitution_before_launch(self) -> None:
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_review_runner_identity",
                return_value=(
                    Path("/private/substitute-code"),
                    f"sha256:{'c' * 64}",
                    self.authorization.review_runner_binary_sha256,
                    REVIEW_RUNNER_BYTES,
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration.subprocess,
                "run",
            ) as run_review,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "drifted from the authorization",
            ),
        ):
            verify_av1_cold_start_preregistration._run_code_agent_review(
                artifact_root=self.runtime_artifact_root,
                plan=self.plan,
                proposal=self._candidate_proposal(),
                lane="architecture",
            )
        run_review.assert_not_called()

    def test_review_runner_is_reverified_after_launch(self) -> None:
        proposal = self._candidate_proposal()
        before_identity = (
            Path("/private/authorized-code"),
            self.authorization.review_runner_canonical_path_sha256,
            self.authorization.review_runner_binary_sha256,
            REVIEW_RUNNER_BYTES,
        )
        after_identity = (
            Path("/private/substitute-code"),
            f"sha256:{'c' * 64}",
            self.authorization.review_runner_binary_sha256,
            REVIEW_RUNNER_BYTES,
        )
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_review_runner_identity",
                side_effect=(before_identity, after_identity),
            ),
            patch.object(
                verify_av1_cold_start_preregistration.subprocess,
                "run",
                return_value=completed,
            ) as run_review,
            patch.object(
                verify_av1_cold_start_preregistration.uuid,
                "uuid4",
                return_value="60000000-0000-0000-0000-000000000001",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_now_iso",
                return_value="2026-07-28T03:00:01Z",
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "drifted from the authorization",
            ),
        ):
            verify_av1_cold_start_preregistration._run_code_agent_review(
                artifact_root=self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                lane="architecture",
            )
        run_review.assert_called_once()
        self.assertEqual(
            len(load_av1_validation_derivation_review_claims(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
            )),
            1,
        )

    @unittest.skipUnless(hasattr(__import__("select"), "kqueue"), "requires kqueue")
    def test_private_review_runner_detects_swap_and_restore(self) -> None:
        expected_sha256 = (
            f"sha256:{hashlib.sha256(REVIEW_RUNNER_BYTES).hexdigest()}"
        )
        runner_directory: Path | None = None
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "changed during review",
        ):
            with verify_av1_cold_start_preregistration._private_review_runner(
                REVIEW_RUNNER_BYTES,
                expected_sha256=expected_sha256,
            ) as runner:
                runner_directory = runner.parent
                substitute = runner.with_name("substitute")
                original = runner.with_name("original")
                substitute.write_bytes(b"substitute-code-binary")
                substitute.chmod(0o500)
                runner.rename(original)
                substitute.rename(runner)
                runner.rename(substitute)
                original.rename(runner)
        assert runner_directory is not None
        self.assertFalse(runner_directory.exists())

    @unittest.skipUnless(hasattr(__import__("select"), "kqueue"), "requires kqueue")
    def test_private_review_runner_detects_write_and_restore(self) -> None:
        expected_sha256 = (
            f"sha256:{hashlib.sha256(REVIEW_RUNNER_BYTES).hexdigest()}"
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "changed during review",
        ):
            with verify_av1_cold_start_preregistration._private_review_runner(
                REVIEW_RUNNER_BYTES,
                expected_sha256=expected_sha256,
            ) as runner:
                runner.chmod(0o700)
                runner.write_bytes(b"substitute-code-binary")
                runner.write_bytes(REVIEW_RUNNER_BYTES)
                runner.chmod(0o500)

    def test_attempt_rejects_persisted_stream_budget_drift(self) -> None:
        assignment = self.plan.assignments[0]
        calibration = _calibration_payload(
            assignment=assignment,
            source_identity=_source_identity(self.partition, assignment),
            crf=28.0,
            compatibility=_compatibility(assignment),
        )
        sample_item = calibration["sample_item"]
        assert isinstance(sample_item, dict)
        persisted_ledger = sample_item["stream_budget_ledger"]
        assert isinstance(persisted_ledger, dict)
        ledger = dict(persisted_ledger)
        totals = dict(ledger["totals"])
        totals["remaining_video_bitrate_bps"] = (
            assignment.target_video_bitrate_bps + 1
        )
        ledger["totals"] = totals
        sample_item["stream_budget_ledger"] = ledger
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "unchanged measured full search",
        ):
            build_av1_validation_derivation_attempt(
                plan=self.plan,
                partition=self.partition,
                assignment_id=assignment.assignment_id,
                started_at="2026-07-28T01:00:00Z",
                completed_at="2026-07-28T01:05:00Z",
                status="review_pending",
                calibration_payload=calibration,
            )

    def test_review_claim_race_leaves_one_terminal_unresolved_claim(self) -> None:
        proposal = self._candidate_proposal()
        claims = [
            build_av1_validation_derivation_review_claim(
                plan=self.plan,
                proposal=proposal,
                lane="architecture",
                review_run_id=f"20000000-0000-0000-0000-{index:012x}",
                review_runner_canonical_path_sha256=(
                    self.authorization.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.authorization.review_runner_binary_sha256
                ),
                claimed_at=f"2026-07-28T03:00:0{index}Z",
            )
            for index in (1, 2)
        ]
        barrier = threading.Barrier(2)

        def write_claim(claim: AV1ValidationDerivationReviewClaim) -> bool:
            barrier.wait()
            try:
                write_av1_validation_derivation_review_claim(
                    self.runtime_artifact_root,
                    plan=self.plan,
                    proposal=proposal,
                    claim=claim,
                )
            except AV1ValidationDerivationError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(write_claim, claims))
        self.assertEqual(sorted(outcomes), [False, True])
        persisted_claims = load_av1_validation_derivation_review_claims(
            self.runtime_artifact_root,
            plan=self.plan,
            proposal=proposal,
        )
        self.assertEqual(len(persisted_claims), 1)
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "all five immutable review claims",
        ):
            finalize_av1_validation_derivation_candidate_lock(
                proposal=proposal,
                review_claims=persisted_claims,
                reviews=(),
                current_evaluation=SimpleNamespace(
                    blockers=(),
                    proposal=proposal,
                    cell_plan_id=proposal.cell_plan_id,
                    derivation_snapshot_sha256=(
                        proposal.derivation_snapshot_sha256
                    ),
                ),
                locked_at=proposal.proposed_at,
            )

    def test_rejected_review_is_persisted_and_cannot_be_replaced(self) -> None:
        proposal = self._candidate_proposal()
        claims = []
        reviews = []
        for index, lane in enumerate(
            AV1_VALIDATION_DERIVATION_REVIEW_LANES,
            start=1,
        ):
            claim = build_av1_validation_derivation_review_claim(
                plan=self.plan,
                proposal=proposal,
                lane=lane,
                review_run_id=f"30000000-0000-0000-0000-{index:012x}",
                review_runner_canonical_path_sha256=(
                    self.authorization.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.authorization.review_runner_binary_sha256
                ),
                claimed_at=f"2026-07-28T03:00:{index:02d}Z",
            )
            decision: Literal["approved", "rejected"] = (
                "rejected" if lane == "privacy_security" else "approved"
            )
            evidence = _review_run_evidence(
                proposal=proposal,
                claim=claim,
                decision=decision,
            )
            review = build_av1_validation_derivation_review_attestation(
                proposal=proposal,
                claim=claim,
                review_evidence_sha256=(
                    f"sha256:{hashlib.sha256(evidence).hexdigest()}"
                ),
                decision=decision,
                reviewed_at=f"2026-07-28T03:{index:02d}:00Z",
            )
            envelope = build_av1_validation_derivation_review_envelope(
                review=review,
                evidence=evidence,
            )
            write_av1_validation_derivation_review_claim(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
            )
            write_av1_validation_derivation_review_envelope(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
                envelope=envelope,
            )
            claims.append(claim)
            reviews.append(review)

        replacement = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            lane="privacy_security",
            review_run_id="40000000-0000-0000-0000-000000000001",
            review_runner_canonical_path_sha256=(
                self.authorization.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=(
                self.authorization.review_runner_binary_sha256
            ),
            claimed_at="2026-07-28T03:10:00Z",
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "already exists",
        ):
            write_av1_validation_derivation_review_claim(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=replacement,
            )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "did not approve",
        ):
            current_proposal = self._candidate_proposal(
                proposed_at="2026-07-28T03:10:00Z"
            )
            finalize_av1_validation_derivation_candidate_lock(
                proposal=proposal,
                review_claims=claims,
                reviews=reviews,
                current_evaluation=SimpleNamespace(
                    blockers=(),
                    proposal=current_proposal,
                    cell_plan_id=current_proposal.cell_plan_id,
                    derivation_snapshot_sha256=(
                        current_proposal.derivation_snapshot_sha256
                    ),
                ),
                locked_at="2026-07-28T03:10:00Z",
            )

    def test_review_evidence_digest_must_match_canonical_evidence(self) -> None:
        proposal = self._candidate_proposal()
        claim = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            lane="architecture",
            review_run_id="50000000-0000-0000-0000-000000000001",
            review_runner_canonical_path_sha256=(
                self.authorization.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=(
                self.authorization.review_runner_binary_sha256
            ),
            claimed_at="2026-07-28T03:00:01Z",
        )
        evidence = _review_run_evidence(proposal=proposal, claim=claim)
        review = build_av1_validation_derivation_review_attestation(
            proposal=proposal,
            claim=claim,
            review_evidence_sha256=f"sha256:{'f' * 64}",
            decision="approved",
            reviewed_at="2026-07-28T03:01:00Z",
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "digest does not match its canonical evidence",
        ):
            validate_av1_validation_derivation_review_run_evidence(
                evidence,
                review=review,
            )

    def test_verdict_retry_reuses_first_immutable_timestamp(self) -> None:
        assignment = self.plan.assignments[0]
        attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            calibration_payload=_calibration_payload(
                assignment=assignment,
                source_identity=_source_identity(self.partition, assignment),
                crf=28.0,
                compatibility=_compatibility(assignment),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            intent_directory = Path(directory) / "verdict-intents"
            first = resolve_av1_validation_derivation_verdict_intent(
                intent_directory,
                plan=self.plan,
                attempt=attempt,
                verdict="approved",
                concern_tags=[],
                evidence_ids=["evidence_test"],
                moment_indexes=[1],
                recorded_at="2026-07-28T01:06:00Z",
            )
            retry = resolve_av1_validation_derivation_verdict_intent(
                intent_directory,
                plan=self.plan,
                attempt=attempt,
                verdict="approved",
                concern_tags=[],
                evidence_ids=["evidence_test"],
                moment_indexes=[1],
                recorded_at="2026-07-28T01:07:00Z",
            )
            self.assertEqual(retry, first)
            self.assertEqual(retry["recorded_at"], "2026-07-28T01:06:00Z")

    def test_candidate_blocks_missing_failed_and_unacceptable_records(self) -> None:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        assignments = [item for item in self.plan.assignments if item.cell_plan_id == cell_plan_id]
        records = [self._observed_record(item.assignment_id, crf=28.0) for item in assignments[:-1]]
        records.append(self._failed_record(assignments[-1].assignment_id))
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=self._current_observations(records),
            proposed_at="2026-07-28T03:00:00Z",
        )
        self.assertIsNone(evaluation.proposal)
        self.assertIn("terminal_failed", evaluation.blockers)

        unacceptable = self._observed_record(assignments[-1].assignment_id, crf=28.0, verdict="unacceptable")
        records[-1] = unacceptable
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=self._current_observations(records),
            proposed_at="2026-07-28T03:00:00Z",
        )
        self.assertIsNone(evaluation.proposal)
        self.assertIn("observation_unacceptable", evaluation.blockers)
        self.assertEqual(evaluation.derivation_conflict_count, 1)

    def test_candidate_rejects_proposal_before_authorization(self) -> None:
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "outside its authorization window",
        ):
            evaluate_av1_validation_derivation_candidate(
                manifest=self.manifest,
                plan=self.plan,
                partition=self.partition,
                cell_plan_id=self.plan.assignments[0].cell_plan_id,
                attempts=(),
                records=(),
                current_observations={},
                proposed_at="2026-07-27T23:59:59Z",
            )

    def test_candidate_rejects_observation_that_is_no_longer_current(self) -> None:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        assignments = [item for item in self.plan.assignments if item.cell_plan_id == cell_plan_id]
        records = [self._observed_record(item.assignment_id, crf=28.0) for item in assignments]
        current_observations = self._current_observations(records)
        del current_observations[assignments[0].assignment_id]
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=current_observations,
            proposed_at="2026-07-28T03:00:00Z",
        )
        self.assertIsNone(evaluation.proposal)
        self.assertIn("observation_not_current", evaluation.blockers)

    def test_candidate_rejects_observation_after_proposal_cutoff(self) -> None:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        assignments = [
            item for item in self.plan.assignments
            if item.cell_plan_id == cell_plan_id
        ]
        records = [
            self._observed_record(
                assignment.assignment_id,
                crf=28.0,
                recorded_at=(
                    "2026-07-28T03:01:00Z"
                    if index == 0
                    else "2026-07-28T01:06:00Z"
                ),
            )
            for index, assignment in enumerate(assignments)
        ]
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=self._current_observations(records),
            proposed_at="2026-07-28T03:00:00Z",
        )
        self.assertIsNone(evaluation.proposal)
        self.assertIn("observation_after_proposal", evaluation.blockers)

    def test_candidate_rejects_forged_terminal_projection(self) -> None:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        assignments = [
            item for item in self.plan.assignments if item.cell_plan_id == cell_plan_id
        ]
        records = [
            self._observed_record(item.assignment_id, crf=28.0)
            for item in assignments
        ]
        attempts = self._attempts(records)
        original = records[0]
        assert original.observation is not None
        forged_projection = replace(original.observation, chosen_crf=35.0)
        assignment = assignments[0]
        semantic_payload = _terminal_semantic_payload(
            plan=self.plan,
            assignment=assignment,
            attempt=attempts[0],
            status="observed",
            reason_code=None,
            observation=forged_projection,
        )
        record_id = _derivation_id("terminal", semantic_payload)
        records[0] = AV1ValidationDerivationTerminalRecord(
            record_id=record_id,
            plan_id=original.plan_id,
            authorization_id=original.authorization_id,
            attempt_id=original.attempt_id,
            attempt_payload_sha256=original.attempt_payload_sha256,
            assignment_id=original.assignment_id,
            cell_plan_id=original.cell_plan_id,
            ordinal=original.ordinal,
            started_at=original.started_at,
            completed_at=original.completed_at,
            status="observed",
            reason_code=None,
            observation=forged_projection,
            payload_sha256=_payload_sha256({
                "record_id": record_id,
                **semantic_payload,
            }),
        )
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=attempts,
            records=records,
            current_observations=self._current_observations([
                self._observed_record(item.assignment_id, crf=28.0)
                for item in assignments
            ]),
            proposed_at="2026-07-28T03:00:00Z",
        )
        self.assertIsNone(evaluation.proposal)
        self.assertIn("terminal_projection_mismatch", evaluation.blockers)

    def test_attempt_rejects_any_warm_start_trace(self) -> None:
        assignment = self.plan.assignments[0]
        payload = _calibration_payload(
            assignment=assignment,
            source_identity=_source_identity(self.partition, assignment),
            crf=28.0,
            compatibility=_compatibility(assignment),
        )
        payload["sample_result"]["target_size_trace"]["warm_start"] = {
            "status": "accepted",
        }
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "unchanged measured full search",
        ):
            build_av1_validation_derivation_attempt(
                plan=self.plan,
                partition=self.partition,
                assignment_id=assignment.assignment_id,
                started_at="2026-07-28T01:00:00Z",
                completed_at="2026-07-28T01:05:00Z",
                status="review_pending",
                calibration_payload=payload,
            )

    def test_attempt_rejects_incomplete_full_search_trace(self) -> None:
        assignment = self.plan.assignments[0]
        payload = _calibration_payload(
            assignment=assignment,
            source_identity=_source_identity(self.partition, assignment),
            crf=28.0,
            compatibility=_compatibility(assignment),
        )
        selected_candidate = payload["sample_result"]["target_size_trace"][
            "selected_candidate"
        ]
        del selected_candidate["sampled_clip_bytes"]
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "unchanged measured full search",
        ):
            build_av1_validation_derivation_attempt(
                plan=self.plan,
                partition=self.partition,
                assignment_id=assignment.assignment_id,
                started_at="2026-07-28T01:00:00Z",
                completed_at="2026-07-28T01:05:00Z",
                status="review_pending",
                calibration_payload=payload,
            )

    def test_terminal_revalidates_loaded_attempt_against_assignment(self) -> None:
        assignment = self.plan.assignments[0]
        calibration = _calibration_payload(
            assignment=assignment,
            source_identity=_source_identity(self.partition, assignment),
            crf=28.0,
            compatibility=_compatibility(assignment),
        )
        calibration["sample_item"]["library_item_id"] = self.plan.assignments[1].local_item_id
        calibration_sha256 = _payload_sha256(calibration)
        semantic_payload = _attempt_semantic_payload(
            plan=self.plan,
            assignment=assignment,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            reason_code=None,
            calibration_payload=calibration,
            calibration_payload_sha256=calibration_sha256,
        )
        attempt_id = _derivation_id("attempt", semantic_payload)
        attempt = AV1ValidationDerivationAttempt(
            attempt_id=attempt_id,
            plan_id=self.plan.plan_id,
            authorization_id=self.plan.authorization.authorization_id,
            assignment_id=assignment.assignment_id,
            cell_plan_id=assignment.cell_plan_id,
            ordinal=assignment.ordinal,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            reason_code=None,
            calibration_payload_json=canonical_json_bytes(calibration).decode("utf-8"),
            calibration_payload_sha256=calibration_sha256,
            payload_sha256=_payload_sha256({"attempt_id": attempt_id, **semantic_payload}),
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "unchanged measured full search",
        ):
            build_av1_validation_derivation_terminal_record(
                plan=self.plan,
                partition=self.partition,
                attempt=attempt,
                observation_exclusion_reason="content_intent_observation_excluded",
            )

    def test_artifact_directories_are_bound_to_one_plan(self) -> None:
        second_authorization = build_av1_validation_v2_derivation_authorization(
            manifest=self.manifest,
            selection_lock_sha256=self.partition.selection_lock_sha256,
            derivation_partition_sha256=self.partition.derivation_partition_sha256,
            runtime_context_sha256=self.authorization.runtime_context_sha256,
            execution_environment_sha256=(
                self.authorization.execution_environment_sha256
            ),
            statistics_contract_sha256=(
                self.authorization.statistics_contract_sha256
            ),
            review_runner_canonical_path_sha256=(
                self.authorization.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=(
                self.authorization.review_runner_binary_sha256
            ),
            authorized_at="2026-07-28T00:01:00Z",
            valid_until=VALID_UNTIL,
        )
        second_plan = build_av1_validation_derivation_plan(
            manifest=self.manifest,
            partition=self.partition,
            authorization=second_authorization,
            runtime_context_sha256=self.plan.runtime_context_sha256,
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "another artifact set",
        ):
            write_av1_validation_derivation_plan(
                self.runtime_artifact_root,
                second_plan,
            )
        first_assignment = self.plan.assignments[0]
        first_attempt = self._failed_attempt(first_assignment.assignment_id)
        second_attempt = build_av1_validation_derivation_attempt(
            plan=second_plan,
            partition=self.partition,
            assignment_id=first_assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="failed",
            reason_code="runtime_failure",
        )
        with tempfile.TemporaryDirectory() as directory:
            attempts_dir = Path(directory) / "attempts"
            write_av1_validation_derivation_attempt(attempts_dir, first_attempt)
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "another artifact set",
            ):
                write_av1_validation_derivation_attempt(attempts_dir, second_attempt)

    def test_unsuccessful_attempt_stops_only_the_affected_cell(self) -> None:
        first_assignment, second_assignment = self.plan.assignments[:2]
        other_cell_assignment = next(
            assignment
            for assignment in self.plan.assignments
            if assignment.cell_plan_id != first_assignment.cell_plan_id
        )
        later_other_cell_assignment = next(
            assignment
            for assignment in self.plan.assignments
            if (
                assignment.cell_plan_id == other_cell_assignment.cell_plan_id
                and assignment.ordinal > other_cell_assignment.ordinal
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts_dir = root / "attempts"
            records_dir = root / "records"
            write_av1_validation_derivation_attempt(
                attempts_dir,
                self._failed_attempt(first_assignment.assignment_id),
            )
            write_av1_validation_derivation_terminal_record(
                records_dir,
                self._failed_record(first_assignment.assignment_id),
            )
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "affected stopped cell",
            ):
                _assert_next_assignment(
                    plan=self.plan,
                    assignment_id=second_assignment.assignment_id,
                    attempts_directory=attempts_dir,
                    terminal_records_directory=records_dir,
                )
            _assert_next_assignment(
                plan=self.plan,
                assignment_id=other_cell_assignment.assignment_id,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
            )
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "not next in the immutable worklist",
            ):
                _assert_next_assignment(
                    plan=self.plan,
                    assignment_id=later_other_cell_assignment.assignment_id,
                    attempts_directory=attempts_dir,
                    terminal_records_directory=records_dir,
                )

    def test_unfavorable_verdict_stops_only_the_affected_cell(self) -> None:
        first_assignment, second_assignment = self.plan.assignments[:2]
        other_cell_assignment = next(
            assignment
            for assignment in self.plan.assignments
            if assignment.cell_plan_id != first_assignment.cell_plan_id
        )
        record = self._observed_record(
            first_assignment.assignment_id,
            crf=28.0,
            verdict="unacceptable",
        )
        attempt = self._attempts((record,))[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts_dir = root / "attempts"
            records_dir = root / "records"
            write_av1_validation_derivation_attempt(attempts_dir, attempt)
            write_av1_validation_derivation_terminal_record(records_dir, record)
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "affected stopped cell",
            ):
                _assert_next_assignment(
                    plan=self.plan,
                    assignment_id=second_assignment.assignment_id,
                    attempts_directory=attempts_dir,
                    terminal_records_directory=records_dir,
                )
            _assert_next_assignment(
                plan=self.plan,
                assignment_id=other_cell_assignment.assignment_id,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
            )

    def test_review_pending_attempt_requires_human_terminal_before_continuing(self) -> None:
        first_assignment, second_assignment = self.plan.assignments[:2]
        attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=first_assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            calibration_payload=_calibration_payload(
                assignment=first_assignment,
                source_identity=_source_identity(self.partition, first_assignment),
                crf=28.0,
                compatibility=_compatibility(first_assignment),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts_dir = root / "attempts"
            write_av1_validation_derivation_attempt(attempts_dir, attempt)
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "prior human visual terminal",
            ):
                _assert_next_assignment(
                    plan=self.plan,
                    assignment_id=second_assignment.assignment_id,
                    attempts_directory=attempts_dir,
                    terminal_records_directory=root / "records",
                )

    def test_next_assignment_rejects_terminal_for_another_attempt(self) -> None:
        first_assignment, second_assignment = self.plan.assignments[:2]
        source_identity = _source_identity(self.partition, first_assignment)
        compatibility = _compatibility(first_assignment)
        persisted_attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=first_assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            calibration_payload=_calibration_payload(
                assignment=first_assignment,
                source_identity=source_identity,
                crf=28.0,
                compatibility=compatibility,
            ),
        )
        other_attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=first_assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            calibration_payload=_calibration_payload(
                assignment=first_assignment,
                source_identity=source_identity,
                crf=29.0,
                compatibility=compatibility,
            ),
        )
        mismatched_record = build_av1_validation_derivation_terminal_record(
            plan=self.plan,
            partition=self.partition,
            attempt=other_attempt,
            observation=_observation(
                assignment=first_assignment,
                source_identity=source_identity,
                crf=29.0,
                bitrate=1_000_000,
                verdict="acceptable",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts_dir = root / "attempts"
            records_dir = root / "records"
            write_av1_validation_derivation_attempt(attempts_dir, persisted_attempt)
            write_av1_validation_derivation_terminal_record(records_dir, mismatched_record)
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "does not match its attempt",
            ):
                _assert_next_assignment(
                    plan=self.plan,
                    assignment_id=second_assignment.assignment_id,
                    attempts_directory=attempts_dir,
                    terminal_records_directory=records_dir,
                )

    def test_next_assignment_requires_immutable_canonical_order(self) -> None:
        first_assignment, second_assignment = self.plan.assignments[:2]
        record = self._observed_record(second_assignment.assignment_id, crf=28.0)
        attempt = self._attempts((record,))[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts_dir = root / "attempts"
            records_dir = root / "records"
            write_av1_validation_derivation_attempt(attempts_dir, attempt)
            write_av1_validation_derivation_terminal_record(records_dir, record)
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "immutable canonical order",
            ):
                _assert_next_assignment(
                    plan=self.plan,
                    assignment_id=first_assignment.assignment_id,
                    attempts_directory=attempts_dir,
                    terminal_records_directory=records_dir,
                )

    def test_orphaned_claim_stops_remaining_worklist(self) -> None:
        assignment = self.plan.assignments[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts_dir = root / "attempts"
            write_av1_validation_derivation_assignment_claim(
                attempts_dir,
                assignment_id=assignment.assignment_id,
                plan_id=self.plan.plan_id,
                authorization_id=self.plan.authorization.authorization_id,
                claimed_at="2026-07-28T01:00:00Z",
            )
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "interrupted claimed assignment",
            ):
                _assert_next_assignment(
                    plan=self.plan,
                    assignment_id=assignment.assignment_id,
                    attempts_directory=attempts_dir,
                    terminal_records_directory=root / "records",
                )

    def test_interrupted_claim_is_terminalized_without_rerunning_media(self) -> None:
        assignment = self.plan.assignments[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts_dir = root / "attempts"
            records_dir = root / "records"
            write_av1_validation_derivation_assignment_claim(
                attempts_dir,
                assignment_id=assignment.assignment_id,
                plan_id=self.plan.plan_id,
                authorization_id=self.plan.authorization.authorization_id,
                claimed_at="2026-07-28T01:00:00Z",
            )
            self.assertTrue(_recover_interrupted_derivation_state(
                plan=self.plan,
                partition=self.partition,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                completed_at="2026-07-28T01:01:00Z",
            ))
            attempt = load_av1_validation_derivation_attempts(attempts_dir)[0]
            terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
            self.assertEqual(attempt.status, "stopped")
            self.assertEqual(attempt.reason_code, "interrupted_claim")
            self.assertEqual(terminal.attempt_id, attempt.attempt_id)

    def test_nonreview_attempt_without_terminal_is_recovered(self) -> None:
        attempt = self._failed_attempt(self.plan.assignments[0].assignment_id)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts_dir = root / "attempts"
            records_dir = root / "records"
            write_av1_validation_derivation_attempt(attempts_dir, attempt)
            self.assertTrue(_recover_interrupted_derivation_state(
                plan=self.plan,
                partition=self.partition,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                completed_at="2026-07-28T01:01:00Z",
            ))
            terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
            self.assertEqual(terminal.attempt_id, attempt.attempt_id)

    def test_runtime_rejects_noncanonical_artifact_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.load_config",
                    return_value=self.runtime_config,
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "partition-global canonical directory",
                ),
            ):
                run_av1_validation_derivation_assignment(
                    config_path=Path("unused.toml"),
                    manifest=self.manifest,
                    partition=self.partition,
                    token_key=self.token_key,
                    plan=self.plan,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    attempts_directory=root / "alternate-attempts",
                    terminal_records_directory=root / "alternate-records",
                )

    def test_execution_contract_rejects_statistics_drift(self) -> None:
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.av1_validation_derivation_statistics_contract_sha256",
                return_value="sha256:" + "f" * 64,
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "statistics contract drifted",
            ),
        ):
            assert_av1_validation_derivation_execution_contract(
                self.manifest,
                self.plan,
            )

    def test_runtime_lock_apis_reject_noncanonical_plan_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            plan_path.write_bytes(canonical_json_bytes(self.plan.to_payload()))
            plan_path.chmod(0o600)
            for operation in (
                finalize_runtime_av1_validation_derivation_candidate_lock,
                load_verified_runtime_av1_validation_derivation_candidate_lock,
            ):
                with self.subTest(operation=operation.__name__):
                    with (
                        patch(
                            "mediaforce.web.runtime.av1_validation_derivation.load_config",
                            return_value=self.runtime_config,
                        ),
                        self.assertRaisesRegex(
                            AV1ValidationDerivationError,
                            "config-derived canonical root",
                        ),
                    ):
                        operation(
                            config_path=Path("unused.toml"),
                            manifest=self.manifest,
                            partition=self.partition,
                            token_key=self.token_key,
                            plan_path=plan_path,
                            cell_plan_id=self.plan.assignments[0].cell_plan_id,
                        )

    def test_derivation_review_media_is_owner_only_and_no_follow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifact-root"
            artifact_root.mkdir(mode=0o700)
            review_root = _prepare_derivation_review_root(artifact_root)
            clip_directory = review_root / "run" / "item-00"
            clip_directory.mkdir(parents=True)
            clip_path = clip_directory / "encoded-01.mp4"
            clip_path.write_bytes(b"review-clip")

            _secure_derivation_review_media(review_root)

            self.assertEqual(review_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(clip_directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(clip_path.stat().st_mode & 0o777, 0o600)
            calibration = {
                "preview_clips": [{
                    "path": clip_path.as_uri(),
                    "timestamp_seconds": 1.0,
                    "duration_seconds": 8.0,
                }],
                "source_clips": [],
            }
            self.assertIsNotNone(
                _current_derivation_review_artifact_fingerprint(
                    review_root=review_root,
                    calibration=calibration,
                )
            )

            linked_clip = clip_directory / "linked.mp4"
            linked_clip.symlink_to(clip_path)
            calibration["preview_clips"][0]["path"] = linked_clip.as_uri()
            self.assertIsNone(
                _current_derivation_review_artifact_fingerprint(
                    review_root=review_root,
                    calibration=calibration,
                )
            )
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "must not contain links",
            ):
                _secure_derivation_review_media(review_root)

    def test_derivation_verdict_validates_terminal_before_database_append(self) -> None:
        assignment = self.plan.assignments[0]
        source = next(
            item
            for item in self.partition.inventory_sources
            if item.local_item_id == assignment.local_item_id
        )
        observation = _observation(
            assignment=assignment,
            source_identity=source.source_identity,
            crf=28.0,
            bitrate=1_000_000,
            verdict="acceptable",
        )
        attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            calibration_payload=_calibration_payload(
                assignment=assignment,
                source_identity=source.source_identity,
                crf=28.0,
                compatibility=_compatibility(assignment),
            ),
        )
        terminal_records_directory = (
            self.runtime_config.paths.web_state_dir
            / "av1-validation-derivation"
            / self.plan.partition_id
            / "terminal-records"
        )
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        connection = SimpleNamespace(exec_driver_sql=lambda _sql: None)
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_config",
                return_value=self.runtime_config,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_db",
                return_value=nullcontext(connection),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory",
                return_value=SimpleNamespace(
                    sources=self.sources,
                    expectations=self.expectations,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._derivation_prefix",
                return_value="private/derivation",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.build_visual_content_intent_observation",
                return_value=ContentIntentObservationBuildResult(observation, None),
            ) as build_observation,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                return_value=attempt.calibration_payload()["review_artifact_fingerprint"],
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.build_av1_validation_derivation_terminal_record",
                side_effect=AV1ValidationDerivationError("terminal validation failed"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.append_content_intent_boundary_observation"
            ) as append_observation,
        ):
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "terminal validation failed",
            ):
                record_av1_validation_derivation_visual_verdict(
                    config_path=Path("unused.toml"),
                    manifest=self.manifest,
                    plan=self.plan,
                    partition=self.partition,
                    token_key=self.token_key,
                    attempt=attempt,
                    terminal_records_directory=terminal_records_directory,
                    verdict="approved",
                    concern_tags=[],
                    evidence_ids=[],
                    moment_indexes=[],
                    recorded_at="2026-07-28T01:06:00Z",
                )
        append_observation.assert_not_called()
        self.assertFalse(build_observation.call_args.kwargs["personalization_eligible"])
        self.assertEqual(
            build_observation.call_args.kwargs["personalization_exclusion_reason"],
            AV1_VALIDATION_DERIVATION_PERSONALIZATION_EXCLUSION_REASON,
        )

    def test_derivation_verdict_does_not_write_terminal_on_observation_conflict(self) -> None:
        assignment = self.plan.assignments[0]
        source_identity = _source_identity(self.partition, assignment)
        observation = _observation(
            assignment=assignment,
            source_identity=source_identity,
            crf=28.0,
            bitrate=1_000_000,
            verdict="acceptable",
        )
        attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            calibration_payload=_calibration_payload(
                assignment=assignment,
                source_identity=source_identity,
                crf=28.0,
                compatibility=_compatibility(assignment),
            ),
        )
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        connection = SimpleNamespace(exec_driver_sql=lambda _sql: None)
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_config",
                return_value=self.runtime_config,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_db",
                return_value=nullcontext(connection),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory",
                return_value=SimpleNamespace(
                    sources=self.sources,
                    expectations=self.expectations,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._derivation_prefix",
                return_value="private/derivation",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.build_visual_content_intent_observation",
                return_value=ContentIntentObservationBuildResult(observation, None),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                return_value=attempt.calibration_payload()["review_artifact_fingerprint"],
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.append_content_intent_boundary_observation",
                side_effect=ContentIntentObservationConflictError("conflict"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.ensure_av1_validation_derivation_terminal_record"
            ) as write_terminal,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "conflicts with existing evidence",
            ),
        ):
            record_av1_validation_derivation_visual_verdict(
                config_path=Path("unused.toml"),
                manifest=self.manifest,
                plan=self.plan,
                partition=self.partition,
                token_key=self.token_key,
                attempt=attempt,
                terminal_records_directory=(
                    self.runtime_artifact_root / "terminal-records"
                ),
                verdict="approved",
                concern_tags=[],
                evidence_ids=[],
                moment_indexes=[],
                recorded_at="2026-07-28T01:06:00Z",
            )
        write_terminal.assert_not_called()

    def test_derivation_verdict_rejects_changed_review_media(self) -> None:
        assignment = self.plan.assignments[0]
        attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            calibration_payload=_calibration_payload(
                assignment=assignment,
                source_identity=_source_identity(self.partition, assignment),
                crf=28.0,
                compatibility=_compatibility(assignment),
            ),
        )
        terminal_records_directory = (
            self.runtime_config.paths.web_state_dir
            / "av1-validation-derivation"
            / self.plan.partition_id
            / "terminal-records"
        )
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        connection = SimpleNamespace(exec_driver_sql=lambda _sql: None)
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_config",
                return_value=self.runtime_config,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_db",
                return_value=nullcontext(connection),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory",
                return_value=SimpleNamespace(
                    sources=self.sources,
                    expectations=self.expectations,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                return_value="changed-review-artifact",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.build_visual_content_intent_observation"
            ) as build_observation,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "review media is unavailable or changed",
            ),
        ):
            record_av1_validation_derivation_visual_verdict(
                config_path=Path("unused.toml"),
                manifest=self.manifest,
                plan=self.plan,
                partition=self.partition,
                token_key=self.token_key,
                attempt=attempt,
                terminal_records_directory=terminal_records_directory,
                verdict="approved",
                concern_tags=[],
                evidence_ids=[],
                moment_indexes=[],
                recorded_at="2026-07-28T01:06:00Z",
            )
        build_observation.assert_not_called()

    def test_derivation_verdict_holds_runtime_lock_through_terminal_commit(self) -> None:
        assignment = self.plan.assignments[0]
        source_identity = _source_identity(self.partition, assignment)
        attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            calibration_payload=_calibration_payload(
                assignment=assignment,
                source_identity=source_identity,
                crf=28.0,
                compatibility=_compatibility(assignment),
            ),
        )
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        observation = _observation(
            assignment=assignment,
            source_identity=source_identity,
            crf=28.0,
            bitrate=1_000_000,
            verdict="acceptable",
        )
        events: list[str] = []
        lock_held = False

        @contextmanager
        def runtime_lock(
                _config: object,
                *,
                owner_payload: object,
        ) -> Iterator[None]:
            nonlocal lock_held
            self.assertIsNotNone(owner_payload)
            lock_held = True
            events.append("lock-enter")
            try:
                yield
            finally:
                events.append("lock-exit")
                lock_held = False

        @contextmanager
        def database(_path: Path) -> Iterator[SimpleNamespace]:
            self.assertTrue(lock_held)
            events.append("db-enter")
            try:
                yield SimpleNamespace(exec_driver_sql=lambda _sql: None)
            finally:
                events.append("db-exit")

        def record_event(label: str) -> None:
            self.assertTrue(lock_held)
            events.append(label)

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_config",
                return_value=self.runtime_config,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.exclusive_mediaforce_runtime_lock",
                side_effect=runtime_lock,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_db",
                side_effect=database,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory",
                return_value=SimpleNamespace(
                    sources=self.sources,
                    expectations=self.expectations,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.validate_av1_validation_partition_current_inputs",
                side_effect=lambda *_args, **_kwargs: record_event(
                    "partition-current"
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract",
                side_effect=lambda *_args, **_kwargs: record_event(
                    "execution-contract"
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                side_effect=lambda **_kwargs: (
                    record_event("media-recheck")
                    or attempt.calibration_payload()["review_artifact_fingerprint"]
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.resolve_av1_validation_derivation_verdict_intent",
                side_effect=lambda *_args, **_kwargs: (
                    record_event("verdict-intent")
                    or {
                        "verdict": "approved",
                        "concern_tags": [],
                        "evidence_ids": [],
                        "moment_indexes": [],
                        "recorded_at": "2026-07-28T01:06:00Z",
                    }
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._derivation_prefix",
                return_value="private/derivation",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.build_visual_content_intent_observation",
                return_value=ContentIntentObservationBuildResult(observation, None),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.ensure_av1_validation_derivation_terminal_intent",
                side_effect=lambda *_args, **_kwargs: record_event("terminal-intent"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.append_content_intent_boundary_observation",
                side_effect=lambda *_args, **_kwargs: record_event("db-append"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.ensure_av1_validation_derivation_terminal_record",
                side_effect=lambda *_args, **_kwargs: record_event("terminal-record"),
            ),
        ):
            terminal = record_av1_validation_derivation_visual_verdict(
                config_path=Path("unused.toml"),
                manifest=self.manifest,
                plan=self.plan,
                partition=self.partition,
                token_key=self.token_key,
                attempt=attempt,
                terminal_records_directory=(
                    self.runtime_artifact_root / "terminal-records"
                ),
                verdict="approved",
                concern_tags=[],
                evidence_ids=[],
                moment_indexes=[],
                recorded_at="2026-07-28T01:06:00Z",
            )
        self.assertEqual(terminal.status, "observed")
        self.assertEqual(events, [
            "lock-enter",
            "db-enter",
            "partition-current",
            "execution-contract",
            "media-recheck",
            "verdict-intent",
            "terminal-intent",
            "db-append",
            "terminal-record",
            "db-exit",
            "execution-contract",
            "lock-exit",
        ])

    def test_derivation_verdict_fails_closed_on_current_input_drift(self) -> None:
        assignment = self.plan.assignments[0]
        attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            calibration_payload=_calibration_payload(
                assignment=assignment,
                source_identity=_source_identity(self.partition, assignment),
                crf=28.0,
                compatibility=_compatibility(assignment),
            ),
        )
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        connection = SimpleNamespace(exec_driver_sql=lambda _sql: None)
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_config",
                return_value=self.runtime_config,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.exclusive_mediaforce_runtime_lock",
                return_value=nullcontext(),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_db",
                return_value=nullcontext(connection),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory",
                return_value=SimpleNamespace(
                    sources=self.sources,
                    expectations=self.expectations,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.validate_av1_validation_partition_current_inputs",
                side_effect=AV1ValidationDerivationError("current inputs drifted"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint"
            ) as review_fingerprint,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.resolve_av1_validation_derivation_verdict_intent"
            ) as verdict_intent,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.append_content_intent_boundary_observation"
            ) as append_observation,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.ensure_av1_validation_derivation_terminal_record"
            ) as write_terminal,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "current inputs drifted",
            ),
        ):
            record_av1_validation_derivation_visual_verdict(
                config_path=Path("unused.toml"),
                manifest=self.manifest,
                plan=self.plan,
                partition=self.partition,
                token_key=self.token_key,
                attempt=attempt,
                terminal_records_directory=(
                    self.runtime_artifact_root / "terminal-records"
                ),
                verdict="approved",
                concern_tags=[],
                evidence_ids=[],
                moment_indexes=[],
                recorded_at="2026-07-28T01:06:00Z",
            )
        review_fingerprint.assert_not_called()
        verdict_intent.assert_not_called()
        append_observation.assert_not_called()
        write_terminal.assert_not_called()

    def test_derivation_observation_is_not_replay_eligible(self) -> None:
        assignment = self.plan.assignments[0]
        source = next(
            item
            for item in self.partition.inventory_sources
            if item.local_item_id == assignment.local_item_id
        )
        observation = _observation(
            assignment=assignment,
            source_identity=source.source_identity,
            crf=28.0,
            bitrate=1_000_000,
            verdict="acceptable",
        )
        state = replay_content_intent_personalization(
            [observation.values()],
            source_id=observation.source_id,
            content_id=observation.content_id,
            prefix=observation.prefix,
            content_profile_id=observation.content_profile_id,
            intent_semantic_id=observation.intent_semantic_id,
            compatibility_key=observation.compatibility_key,
        )
        self.assertEqual(state.item_boundary.status, "empty")
        self.assertTrue(all(cohort.observation_count == 0 for cohort in state.cohorts))

        forged_values = observation.values()
        forged_values["personalization_eligible"] = True
        forged_values["exclusion_reason"] = None
        provenance = json.loads(str(forged_values["provenance_json"]))
        provenance["calibration_action"] = "av1_derivation"
        forged_values["provenance_json"] = canonical_json_bytes(provenance).decode(
            "utf-8"
        )
        forged = _rehash_observation(forged_values)
        forged_state = replay_content_intent_personalization(
            [forged.values()],
            source_id=forged.source_id,
            content_id=forged.content_id,
            prefix=forged.prefix,
            content_profile_id=forged.content_profile_id,
            intent_semantic_id=forged.intent_semantic_id,
            compatibility_key=forged.compatibility_key,
        )
        self.assertEqual(forged_state.item_boundary.status, "empty")

        withdrawn = withdraw_content_intent_boundary_observation(
            observation,
            reason_code="operator_invalidated_derivation_evidence",
            recorded_at="2026-07-28T01:07:00Z",
        )
        self.assertEqual(withdrawn.disposition, "withdrawn")
        self.assertEqual(
            withdrawn.exclusion_reason,
            AV1_VALIDATION_DERIVATION_PERSONALIZATION_EXCLUSION_REASON,
        )
        self.assertEqual(
            withdrawn.supersession_reason,
            "operator_invalidated_derivation_evidence",
        )

        with self.assertRaisesRegex(
            ValueError,
            "quarantine cannot be lifted",
        ):
            correct_content_intent_boundary_observation(
                observation,
                verdict="unacceptable",
                personalization_eligible=True,
                exclusion_reason=None,
                reason_code="operator_reassessed_same_review",
                recorded_at="2026-07-28T01:05:00Z",
            )

    def test_candidate_uses_assignment_bitrate_range_and_observed_dispersion(self) -> None:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        assignments = [item for item in self.plan.assignments if item.cell_plan_id == cell_plan_id]
        records = [
            self._observed_record(
                assignment.assignment_id,
                crf=27.0 if index < 6 else 29.0,
                bitrate=850_000 if index < 6 else 1_150_000,
            )
            for index, assignment in enumerate(assignments)
        ]
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=self._current_observations(records),
            proposed_at="2026-07-28T03:00:00Z",
        )
        self.assertEqual(evaluation.blockers, ())
        assert evaluation.proposal is not None
        self.assertEqual(evaluation.proposal.crf_lower, 27.0)
        self.assertEqual(evaluation.proposal.crf_center, 28.0)
        self.assertEqual(evaluation.proposal.crf_upper, 29.0)
        self.assertEqual(evaluation.proposal.crf_mad, 1.0)
        self.assertEqual(evaluation.proposal.bitrate_relative_mad, 0.15)
        self.assertEqual(
            evaluation.proposal.statistics_contract_sha256,
            self.authorization.statistics_contract_sha256,
        )
        self.assertEqual(evaluation.proposal.derivation_conflict_count, 0)
        self.assertEqual(evaluation.proposal.confidence_level, "moderate")
        self.assertEqual(evaluation.proposal.confidence_score, 0.85)
        self.assertEqual(
            evaluation.proposal.target_video_bitrate_min_bps,
            min(assignment.target_video_bitrate_bps for assignment in assignments),
        )
        self.assertEqual(
            evaluation.proposal.target_video_bitrate_max_bps,
            max(assignment.target_video_bitrate_bps for assignment in assignments),
        )

    def test_candidate_rejects_full_crf_span_even_when_center_is_tight(self) -> None:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        assignments = [item for item in self.plan.assignments if item.cell_plan_id == cell_plan_id]
        crfs = [24.0, *([28.0] * 10), 31.0]
        records = [
            self._observed_record(assignment.assignment_id, crf=crf)
            for assignment, crf in zip(assignments, crfs, strict=True)
        ]
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=self._current_observations(records),
            proposed_at="2026-07-28T03:00:00Z",
        )
        self.assertIsNone(evaluation.proposal)
        self.assertIn("crf_span_too_wide", evaluation.blockers)

    def test_proposal_type_reasserts_preregistered_invariants(self) -> None:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        assignments = [
            assignment
            for assignment in self.plan.assignments
            if assignment.cell_plan_id == cell_plan_id
        ]
        records = [
            self._observed_record(assignment.assignment_id, crf=28.0)
            for assignment in assignments
        ]
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=self._current_observations(records),
            proposed_at="2026-07-28T03:00:00Z",
        )
        assert evaluation.proposal is not None
        proposal = evaluation.proposal

        for field_updates, message in (
            ({"crf_lower": 24.0, "crf_upper": 31.0}, "CRF span is too wide"),
            ({"minimum_derivation_source_count": 5}, "source-count contract"),
            (
                {"derivation_series_tokens": proposal.derivation_series_tokens[:-1]},
                "unique source, title, and series reservations",
            ),
            (
                {"derivation_oldest_recorded_at": "2026-01-01T00:00:00Z"},
                "evidence is stale",
            ),
        ):
            with self.subTest(field_updates=field_updates), self.assertRaisesRegex(
                AV1ValidationDerivationError,
                message,
            ):
                replace(proposal, **field_updates)

    def test_candidate_rejects_wide_crf_dispersion_with_acceptable_span(self) -> None:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        assignments = [
            item for item in self.plan.assignments
            if item.cell_plan_id == cell_plan_id
        ]
        crfs = [26.0] * 6 + [31.0] * 6
        records = [
            self._observed_record(assignment.assignment_id, crf=crf)
            for assignment, crf in zip(assignments, crfs, strict=True)
        ]
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=self._current_observations(records),
            proposed_at="2026-07-28T03:00:00Z",
        )
        self.assertIsNone(evaluation.proposal)
        self.assertIn("crf_dispersion_too_wide", evaluation.blockers)
        self.assertNotIn("crf_span_too_wide", evaluation.blockers)

    def test_candidate_rejects_limited_confidence(self) -> None:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        assignments = [
            item for item in self.plan.assignments
            if item.cell_plan_id == cell_plan_id
        ]
        records = [
            self._observed_record(
                assignment.assignment_id,
                crf=28.0,
                bitrate=500_000 if index < 6 else 1_500_000,
            )
            for index, assignment in enumerate(assignments)
        ]
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=self._current_observations(records),
            proposed_at="2026-07-28T03:00:00Z",
        )
        self.assertIsNone(evaluation.proposal)
        self.assertIn("confidence_insufficient", evaluation.blockers)

    def test_five_distinct_approvals_are_required_before_lock_finalization(self) -> None:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        assignments = [item for item in self.plan.assignments if item.cell_plan_id == cell_plan_id]
        records = [self._observed_record(item.assignment_id, crf=28.0) for item in assignments]
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=self._current_observations(records),
            proposed_at="2026-07-28T03:00:00Z",
        )
        assert evaluation.proposal is not None
        review_evidence: dict[str, bytes] = {}
        review_claims = []
        reviews = []
        for index, lane in enumerate(
            AV1_VALIDATION_DERIVATION_REVIEW_LANES,
            start=1,
        ):
            claim = build_av1_validation_derivation_review_claim(
                plan=self.plan,
                proposal=evaluation.proposal,
                lane=lane,
                review_run_id=f"00000000-0000-0000-0000-{index:012x}",
                review_runner_canonical_path_sha256=(
                    self.authorization.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.authorization.review_runner_binary_sha256
                ),
                claimed_at=f"2026-07-28T03:00:{index:02d}Z",
            )
            review_claims.append(claim)
            evidence = _review_run_evidence(
                proposal=evaluation.proposal,
                claim=claim,
            )
            review_evidence[lane] = evidence
            reviews.append(build_av1_validation_derivation_review_attestation(
                proposal=evaluation.proposal,
                claim=claim,
                review_evidence_sha256=(
                    f"sha256:{hashlib.sha256(evidence).hexdigest()}"
                ),
                decision="approved",
                reviewed_at=f"2026-07-28T03:{index:02d}:00Z",
            ))
        locked_at = "2026-07-28T03:06:00Z"
        current_evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=self._current_observations(records),
            proposed_at=locked_at,
        )
        with self.assertRaisesRegex(AV1ValidationDerivationError, "all five"):
            finalize_av1_validation_derivation_candidate_lock(
                proposal=evaluation.proposal,
                review_claims=review_claims,
                reviews=reviews[:-1],
                current_evaluation=current_evaluation,
                locked_at=locked_at,
            )
        duplicate_evidence_claims = [
            build_av1_validation_derivation_review_claim(
                plan=self.plan,
                proposal=evaluation.proposal,
                lane=lane,
                review_run_id=f"10000000-0000-0000-0000-{index:012x}",
                review_runner_canonical_path_sha256=(
                    self.authorization.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.authorization.review_runner_binary_sha256
                ),
                claimed_at=f"2026-07-28T03:00:{index:02d}Z",
            )
            for index, lane in enumerate(
                AV1_VALIDATION_DERIVATION_REVIEW_LANES,
                start=1,
            )
        ]
        duplicate_evidence_reviews = [
            build_av1_validation_derivation_review_attestation(
                proposal=evaluation.proposal,
                claim=claim,
                review_evidence_sha256=f"sha256:{1:064x}",
                decision="approved",
                reviewed_at=f"2026-07-28T03:{index:02d}:30Z",
            )
            for index, claim in enumerate(duplicate_evidence_claims, start=1)
        ]
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "independent agent runs",
        ):
            finalize_av1_validation_derivation_candidate_lock(
                proposal=evaluation.proposal,
                review_claims=duplicate_evidence_claims,
                reviews=duplicate_evidence_reviews,
                current_evaluation=current_evaluation,
                locked_at=locked_at,
            )
        lock = finalize_av1_validation_derivation_candidate_lock(
            proposal=evaluation.proposal,
            review_claims=review_claims,
            reviews=reviews,
            current_evaluation=current_evaluation,
            locked_at=locked_at,
        )
        self.assertEqual(lock.review_state, "approved_for_holdout")
        self.assertEqual(lock.derivation_snapshot_sha256, evaluation.derivation_snapshot_sha256)
        self.assertEqual(lock.locked_at, locked_at)
        self.assertEqual(lock.reviewed_at, locked_at)

        with tempfile.TemporaryDirectory() as directory:
            artifact_root = (
                Path(directory)
                / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
                / self.plan.partition_id
            )
            write_av1_validation_derivation_plan(artifact_root, self.plan)
            proposal_path = write_av1_validation_derivation_candidate_proposal(
                artifact_root,
                plan=self.plan,
                proposal=evaluation.proposal,
            )
            self.assertEqual(
                proposal_path,
                (artifact_root / "proposals" / f"{cell_plan_id}.json").resolve(),
            )
            self.assertEqual(
                load_av1_validation_derivation_candidate_proposal(
                    artifact_root,
                    plan=self.plan,
                    cell_plan_id=cell_plan_id,
                ),
                evaluation.proposal,
            )
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "not authorized",
            ):
                load_av1_validation_derivation_candidate_proposal(
                    artifact_root,
                    plan=self.plan,
                    cell_plan_id="../outside",
                )
            review_envelopes = []
            for claim, review in zip(review_claims, reviews, strict=True):
                write_av1_validation_derivation_review_claim(
                    artifact_root,
                    plan=self.plan,
                    proposal=evaluation.proposal,
                    claim=claim,
                )
                envelope = build_av1_validation_derivation_review_envelope(
                    review=review,
                    evidence=review_evidence[review.lane],
                )
                write_av1_validation_derivation_review_envelope(
                    artifact_root,
                    plan=self.plan,
                    proposal=evaluation.proposal,
                    claim=claim,
                    envelope=envelope,
                )
                review_envelopes.append(envelope)
            stale_temporary = (
                artifact_root
                / "reviews"
                / evaluation.proposal.proposal_id
                / ".architecture.json.interrupted.tmp"
            )
            stale_temporary.write_bytes(b"interrupted")
            stale_temporary.chmod(0o600)
            loaded_claims = load_av1_validation_derivation_review_claims(
                artifact_root,
                plan=self.plan,
                proposal=evaluation.proposal,
            )
            loaded_envelopes = load_av1_validation_derivation_review_envelopes(
                artifact_root,
                plan=self.plan,
                proposal=evaluation.proposal,
                claims=loaded_claims,
            )
            self.assertEqual(
                {
                    envelope.review.attestation_id
                    for envelope in loaded_envelopes
                },
                {review.attestation_id for review in reviews},
            )
            persisted_envelope = finalize_and_write_av1_validation_derivation_candidate_lock(
                artifact_root,
                plan=self.plan,
                proposal=evaluation.proposal,
                review_claims=loaded_claims,
                review_envelopes=loaded_envelopes,
                current_evaluation=current_evaluation,
                locked_at=locked_at,
            )
            self.assertEqual(persisted_envelope.candidate_lock, lock)
            self.assertEqual(
                load_verified_av1_validation_derivation_candidate_lock(
                    artifact_root,
                    plan=self.plan,
                    proposal=evaluation.proposal,
                    review_claims=loaded_claims,
                    review_envelopes=loaded_envelopes,
                    current_evaluation=current_evaluation,
                    cell_plan_id=cell_plan_id,
                ),
                persisted_envelope,
            )
            lock_path = (
                artifact_root / "candidate-locks" / f"{cell_plan_id}.json"
            ).resolve()
            self.assertEqual(
                lock_path,
                (artifact_root / "candidate-locks" / f"{cell_plan_id}.json").resolve(),
            )

        changed_observations = self._current_observations(records)
        del changed_observations[assignments[0].assignment_id]
        stale_evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=changed_observations,
            proposed_at=locked_at,
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "no longer current",
        ):
            finalize_av1_validation_derivation_candidate_lock(
                proposal=evaluation.proposal,
                review_claims=review_claims,
                reviews=reviews,
                current_evaluation=stale_evaluation,
                locked_at=locked_at,
            )

    def test_candidate_lock_allows_shared_source_groups(self) -> None:
        source_tokens = tuple(f"source_token_{index:02d}" for index in range(12))
        series_tokens = tuple(f"series_token_{index:02d}" for index in range(12))
        shared_tokens = tuple(f"shared_token_{index:02d}" for index in range(6))
        candidate_lock_kwargs = {
            "manifest_id": self.manifest.manifest_id,
            "cell_plan_id": self.plan.assignments[0].cell_plan_id,
            "exact_traits": ("darkness",),
            "crf_lower": 27.0,
            "crf_center": 28.0,
            "crf_upper": 29.0,
            "compatibility_signature": "compatibility_test_contract",
            "policy_signature": "policy_test_contract",
            "target_video_bitrate_min_bps": 850_000,
            "target_video_bitrate_max_bps": 1_150_000,
            "minimum_quality_score": 80.0,
            "confidence_level": "moderate",
            "confidence_score": 0.85,
            "derivation_evidence_count": 12,
            "derivation_source_count": 12,
            "derivation_source_tokens": source_tokens,
            "derivation_title_tokens": tuple(
                f"title_{index:02d}_independent" for index in range(12)
            ),
            "derivation_series_tokens": series_tokens,
            "derivation_source_group_tokens": shared_tokens,
            "derivation_source_group_observation_tokens": tuple(
                token for token in shared_tokens for _ in range(2)
            ),
            "derivation_oldest_recorded_at": "2026-07-28T01:00:00Z",
            "derivation_newest_recorded_at": "2026-07-28T02:00:00Z",
            "derivation_conflict_count": 0,
            "derivation_snapshot_sha256": "sha256:" + "1" * 64,
            "selection_lock_sha256": self.plan.selection_lock_sha256,
            "locked_at": "2026-07-28T03:00:00Z",
            "reviewed_at": "2026-07-28T04:00:00Z",
        }
        candidate_lock = build_av1_cold_start_validation_candidate_lock(
            **candidate_lock_kwargs,
        )
        self.assertEqual(candidate_lock.derivation_source_count, 12)
        self.assertEqual(len(candidate_lock.derivation_source_group_tokens), 6)
        self.assertEqual(
            len(candidate_lock.derivation_source_group_observation_tokens),
            12,
        )

        with self.assertRaisesRegex(
            AV1ColdStartValidationError,
            "at least six source groups",
        ):
            build_av1_cold_start_validation_candidate_lock(
                **{
                    **candidate_lock_kwargs,
                    "derivation_source_group_tokens": shared_tokens[:5],
                    "derivation_source_group_observation_tokens": tuple(
                        shared_tokens[index % 5] for index in range(12)
                    ),
                }
            )

        concentrated_tokens = tuple(f"concentrated_{index:02d}" for index in range(8))
        with self.assertRaisesRegex(
            AV1ColdStartValidationError,
            "concentration is too high",
        ):
            build_av1_cold_start_validation_candidate_lock(
                **{
                    **candidate_lock_kwargs,
                    "derivation_source_group_tokens": concentrated_tokens,
                    "derivation_source_group_observation_tokens": (
                        (concentrated_tokens[0],) * 5
                        + concentrated_tokens[1:]
                    ),
                }
            )

        with self.assertRaisesRegex(
            AV1ColdStartValidationError,
            "CRF span is too wide",
        ):
            build_av1_cold_start_validation_candidate_lock(
                **{
                    **candidate_lock_kwargs,
                    "crf_lower": 24.0,
                    "crf_upper": 31.0,
                }
            )

        with self.assertRaisesRegex(
            AV1ColdStartValidationError,
            "series tokens are incomplete",
        ):
            build_av1_cold_start_validation_candidate_lock(
                **{
                    **candidate_lock_kwargs,
                    "derivation_series_tokens": series_tokens[:-1],
                }
            )

        with self.assertRaisesRegex(
            AV1ColdStartValidationError,
            "below the preregistered minimum",
        ):
            build_av1_cold_start_validation_candidate_lock(
                **{
                    **candidate_lock_kwargs,
                    "derivation_source_count": 5,
                    "derivation_source_tokens": source_tokens[:5],
                }
            )

        with self.assertRaisesRegex(
            AV1ColdStartValidationError,
            "evidence is stale",
        ):
            build_av1_cold_start_validation_candidate_lock(
                **{
                    **candidate_lock_kwargs,
                    "locked_at": "2027-02-01T03:00:00Z",
                    "reviewed_at": "2027-02-01T03:00:00Z",
                }
            )

    def test_create_plan_cli_output_is_public_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private"
            root.mkdir(mode=0o700)
            runtime_config = SimpleNamespace(
                paths=SimpleNamespace(
                    db_path=root / "db.sqlite3",
                    review_dir=root / "review",
                    web_state_dir=root / "state",
                )
            )
            stdout = io.StringIO()
            with (
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_load_current_derivation_inputs",
                    return_value=(self.manifest, self.partition, self.token_key),
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "load_config",
                    return_value=runtime_config,
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_now_iso",
                    return_value=AUTHORIZED_AT,
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_review_runner_identity",
                    return_value=(
                        root / "private-code-runner",
                        self.authorization.review_runner_canonical_path_sha256,
                        self.authorization.review_runner_binary_sha256,
                        REVIEW_RUNNER_BYTES,
                    ),
                ),
                redirect_stdout(stdout),
            ):
                exit_code = verify_av1_cold_start_preregistration.main([
                    "create-derivation-plan",
                    str(V2_MANIFEST_PATH),
                    str(root / "eligibility.json"),
                    str(root / "partition.json"),
                    "--key",
                    str(root / "partition.key"),
                    "--valid-until",
                    VALID_UNTIL,
                    "--json",
                ])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["derivation_assignment_count"], 24)
            plan_path = (
                runtime_config.paths.web_state_dir
                / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
                / self.partition.partition_id
                / "plan.json"
            )
            self.assertTrue(plan_path.is_file())
            serialized = json.dumps(payload)
            self.assertNotIn("local_item_id", serialized)
            self.assertNotIn("source_token", serialized)
            self.assertNotIn(str(root), serialized)

    def _candidate_proposal(
            self,
            *,
            proposed_at: str = "2026-07-28T03:00:00Z",
    ) -> AV1ValidationDerivationCandidateProposal:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        assignments = [
            assignment
            for assignment in self.plan.assignments
            if assignment.cell_plan_id == cell_plan_id
        ]
        records = [
            self._observed_record(assignment.assignment_id, crf=28.0)
            for assignment in assignments
        ]
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=self._attempts(records),
            records=records,
            current_observations=self._current_observations(records),
            proposed_at=proposed_at,
        )
        if evaluation.proposal is None:
            raise AssertionError(evaluation.blockers)
        return evaluation.proposal

    def _failed_attempt(self, assignment_id: str) -> AV1ValidationDerivationAttempt:
        return build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="failed",
            reason_code="runtime_failure",
        )

    def _failed_record(self, assignment_id: str) -> AV1ValidationDerivationTerminalRecord:
        return build_av1_validation_derivation_terminal_record(
            plan=self.plan,
            partition=self.partition,
            attempt=self._failed_attempt(assignment_id),
        )

    def _observed_record(
            self,
            assignment_id: str,
            *,
            crf: float,
            bitrate: int = 1_000_000,
            verdict: Literal["acceptable", "unacceptable"] = "acceptable",
            recorded_at: str = "2026-07-28T01:06:00Z",
    ) -> AV1ValidationDerivationTerminalRecord:
        assignment = next(item for item in self.plan.assignments if item.assignment_id == assignment_id)
        source = next(item for item in self.partition.inventory_sources if item.local_item_id == assignment.local_item_id)
        observation = _observation(
            assignment=assignment,
            source_identity=source.source_identity,
            crf=crf,
            bitrate=bitrate,
            verdict=verdict,
            recorded_at=recorded_at,
        )
        attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            calibration_payload=_calibration_payload(
                assignment=assignment,
                source_identity=source.source_identity,
                crf=crf,
                compatibility=_compatibility(assignment),
                bitrate=bitrate,
            ),
        )
        return build_av1_validation_derivation_terminal_record(
            plan=self.plan,
            partition=self.partition,
            attempt=attempt,
            observation=observation,
        )

    def _attempts(
            self,
            records: Sequence[AV1ValidationDerivationTerminalRecord],
    ) -> list[AV1ValidationDerivationAttempt]:
        attempts: list[AV1ValidationDerivationAttempt] = []
        for record in records:
            if record.status == "observed":
                assert record.observation is not None
                assignment = next(
                    item for item in self.plan.assignments
                    if item.assignment_id == record.assignment_id
                )
                attempts.append(
                    build_av1_validation_derivation_attempt(
                        plan=self.plan,
                        partition=self.partition,
                        assignment_id=record.assignment_id,
                        started_at=record.started_at,
                        completed_at=record.completed_at,
                        status="review_pending",
                        calibration_payload=_calibration_payload(
                            assignment=assignment,
                            source_identity=_source_identity(self.partition, assignment),
                            crf=record.observation.chosen_crf,
                            compatibility=_compatibility(assignment),
                            bitrate=record.observation.boundary_bitrate_bps,
                        ),
                    )
                )
            else:
                attempts.append(
                    build_av1_validation_derivation_attempt(
                        plan=self.plan,
                        partition=self.partition,
                        assignment_id=record.assignment_id,
                        started_at=record.started_at,
                        completed_at=record.completed_at,
                        status=record.status,
                        reason_code=record.reason_code,
                    )
                )
        return attempts

    def _current_observations(
            self,
            records: Sequence[AV1ValidationDerivationTerminalRecord],
    ) -> dict[str, ContentIntentBoundaryObservation]:
        observations: dict[str, ContentIntentBoundaryObservation] = {}
        for record in records:
            if record.observation is None:
                continue
            assignment = next(
                item
                for item in self.plan.assignments
                if item.assignment_id == record.assignment_id
            )
            observations[record.assignment_id] = _observation(
                assignment=assignment,
                source_identity=_source_identity(self.partition, assignment),
                crf=record.observation.chosen_crf,
                bitrate=record.observation.boundary_bitrate_bps,
                verdict=record.observation.verdict,
                recorded_at=record.observation.recorded_at,
            )
        return observations


def _observation(
        *,
        assignment: AV1ValidationPartitionAssignment,
        source_identity: str,
        crf: float,
        bitrate: int,
        verdict: Literal["acceptable", "unacceptable"],
        recorded_at: str = "2026-07-28T01:06:00Z",
) -> ContentIntentBoundaryObservation:
    compatibility = _compatibility(assignment)
    acceptable = verdict == "acceptable"
    return _build_observation(
        series_id=f"series_{assignment.assignment_id}",
        boundary_group_id=f"group_{assignment.assignment_id}",
        revision=0,
        supersedes_observation_id=None,
        supersession_reason=None,
        authority="runtime_native",
        disposition="active",
        personalization_eligible=False,
        exclusion_reason=AV1_VALIDATION_DERIVATION_PERSONALIZATION_EXCLUSION_REASON,
        library_item_id=assignment.local_item_id,
        prefix="private/derivation",
        source_rel_path="private/derivation/item.mkv",
        source_id=f"source_{assignment.assignment_id}",
        source_fingerprint="fingerprint_test",
        content_fingerprint=source_identity,
        content_id=f"content_{assignment.assignment_id}",
        content_profile_id=f"profile_{assignment.assignment_id}",
        content_traits=assignment.traits,
        intent_semantic_id=f"intent_{assignment.assignment_id}",
        intent_snapshot_id=f"snapshot_{assignment.assignment_id}",
        intent_level=assignment.intent_level,
        compatibility=compatibility,
        policy_hash="policy_hash_test_contract",
        source_event_kind="post_test_review",
        source_event_id=f"event_{assignment.assignment_id}",
        job_id=f"job_{assignment.assignment_id}",
        artifact_fingerprint=f"artifact_{assignment.assignment_id}",
        source_evidence_ids=("evidence_test",),
        observation_kind="visual_approval" if acceptable else "visual_rejection",
        verdict="acceptable" if acceptable else "unacceptable",
        boundary_kind="upper_bound" if acceptable else "lower_bound",
        authoritative_anchor_bytes=500_000_000,
        boundary_size_bytes=450_000_000,
        actual_output_bytes=None,
        sampled_clip_bytes=5_000_000,
        duration_seconds=3_600.0,
        boundary_bitrate_bps=bitrate,
        direction="smaller",
        quality_metric=assignment.quality_metric,
        quality_target=assignment.quality_target,
        minimum_quality_score=assignment.minimum_quality_score,
        measured_quality_score=assignment.minimum_quality_score + 1.0,
        quality_floor_met=True,
        assessment={
            "schema_version": 1,
            "measurement_basis": "sample_projection",
            "chosen_crf": crf,
        },
        provenance={
            "schema_version": 1,
            "source": "operator_visual_review",
            "recorded_at": recorded_at,
        },
        recorded_at=recorded_at,
    )


def _compatibility(
        assignment: AV1ValidationPartitionAssignment,
) -> ContentIntentBoundaryCompatibilityV1:
    return build_content_intent_boundary_compatibility(
        encoder="libsvtav1",
        encoder_version="4.2.0",
        encoder_runtime_version="8.1.2",
        encoder_runtime_signature_id="encoder_runtime_test",
        quality_tool="ab-av1",
        quality_tool_version="0.11.3",
        metric_runtime_signature_id="metric_runtime_test",
        preset=6,
        pixel_format="yuv420p10le",
        encoder_parameters=("preset=6",),
        output_width=1920,
        output_height=1080,
        frame_rate="24000/1001",
        cadence_transform="none",
        video_filter=None,
        output_container="mkv",
        stream_plan_id="stream_plan_test",
        measurement_basis="sample_projection",
        quality_metric=assignment.quality_metric,
        quality_target=assignment.quality_target,
        minimum_quality_score=assignment.minimum_quality_score,
    )


def _calibration_payload(
        *,
        assignment: AV1ValidationPartitionAssignment,
        source_identity: str,
        crf: float,
        compatibility: ContentIntentBoundaryCompatibilityV1,
        bitrate: int = 1_000_000,
) -> dict[str, object]:
    artifact = f"artifact_{assignment.assignment_id}"
    duration_seconds = 3_600.0
    predicted_video_bytes = round((bitrate * duration_seconds) / 8)
    candidate = {
        "attempt": 1,
        "role": "target_seed",
        "crf": crf,
        "metric": assignment.quality_metric.upper(),
        "metric_target": assignment.quality_target,
        "metric_score": assignment.minimum_quality_score + 1.0,
        "min_metric_score": assignment.minimum_quality_score,
        "quality_floor_met": True,
        "sampled_clip_bytes": 5_000_000,
        "predicted_video_bytes": predicted_video_bytes,
        "predicted_whole_episode_bytes": predicted_video_bytes,
        "predicted_encode_percent": 50.0,
        "predicted_encode_seconds": 1_800.0,
        "target_distance_bytes": 0,
        "within_sample_band": True,
        "violates_source_cap": False,
    }
    return {
        "mode": "sample",
        "action": "av1_derivation",
        "host": {
            "mode": "local",
            "media_access": "direct",
        },
        "job_id": f"job_{assignment.assignment_id}",
        "review_media_ready": True,
        "boundary_review_media_ready": True,
        "review_artifact_fingerprint": artifact,
        "current_review_artifact_fingerprint": artifact,
        "sample_item": {
            "library_item_id": assignment.local_item_id,
            "content_version_fingerprint": source_identity,
            "duration_seconds": duration_seconds,
            "stream_budget_ledger": {
                "schema_version": 1,
                "totals": {
                    "remaining_video_bitrate_bps": (
                        assignment.target_video_bitrate_bps
                    ),
                },
            },
        },
        "sample_result": {
            "chosen_crf": crf,
            "quality_metric": assignment.quality_metric,
            "quality_target": assignment.quality_target,
            "quality_score": assignment.minimum_quality_score + 1.0,
            "predicted_video_size_bytes": predicted_video_bytes,
            "predicted_total_size_bytes": predicted_video_bytes,
            "content_intent_compatibility": compatibility.to_payload(),
            "av1_cold_start_prior": {
                "status": "unavailable",
                "reason": "cold_start_planner_unavailable",
                "execution": None,
            },
            "target_size_trace": {
                "schema_version": 1,
                "status": "selected",
                "selection_reason": "inside_target_band",
                "quality_floor": {
                    "metric": assignment.quality_metric.upper(),
                    "target": assignment.quality_target,
                    "minimum": assignment.minimum_quality_score,
                },
                "curve": {
                    "shape": "single_point",
                    "candidate_count": 1,
                    "max_candidates": 6,
                },
                "retry_policy": {
                    "max_final_output_retries": 1,
                },
                "candidates": [candidate],
                "selected_candidate": candidate,
            },
        },
    }


def _review_run_evidence(
        *,
        proposal: object,
        claim: AV1ValidationDerivationReviewClaim,
        decision: Literal["approved", "rejected"] = "approved",
) -> bytes:
    proposal_id = str(getattr(proposal, "proposal_id"))
    proposal_payload_sha256 = str(getattr(proposal, "payload_sha256"))
    lane = str(getattr(claim, "lane"))
    review_run_id = str(getattr(claim, "review_run_id"))
    review_claim_id = str(getattr(claim, "claim_id"))
    review_claim_payload_sha256 = str(getattr(claim, "payload_sha256"))
    prompt = (
        f"review_run_id={review_run_id}\n"
        f"proposal_id={proposal_id}\n"
        f"proposal_payload_sha256={proposal_payload_sha256}\n"
        f"review_claim_id={review_claim_id}\n"
        f"review_claim_payload_sha256={review_claim_payload_sha256}\n"
        f"lane={lane}"
    )
    marker = {
        "decision": decision,
        "lane": lane,
        "proposal_id": proposal_id,
        "proposal_payload_sha256": proposal_payload_sha256,
        "review_claim_id": review_claim_id,
        "review_claim_payload_sha256": review_claim_payload_sha256,
        "review_run_id": review_run_id,
    }
    final_message = (
        "Review complete.\n"
        "MEDIAFORCE_AV1_REVIEW_V2 "
        f"{json.dumps(marker, sort_keys=True, separators=(',', ':'))}"
    )
    stdout = "\n".join((
        json.dumps({
            "provider": "test",
            "model": "test-model",
            "workdir": "/private/test",
            "approval": "never",
            "sandbox": "read-only",
        }, sort_keys=True),
        json.dumps({"prompt": prompt}, sort_keys=True),
        json.dumps({
            "msg": {
                "type": "agent_message",
                "message": final_message,
            }
        }, sort_keys=True),
        json.dumps({
            "msg": {
                "type": "task_lifecycle",
                "phase": "quiescent",
                "last_agent_message": final_message,
            }
        }, sort_keys=True),
    ))
    return canonical_json_bytes({
        "schema": "mediaforce.av1_derivation_agent_review_run",
        "schema_version": 1,
        "review_run_id": review_run_id,
        "reviewer_token": f"agent:{review_run_id}",
        "proposal_id": proposal_id,
        "proposal_payload_sha256": proposal_payload_sha256,
        "review_claim_id": review_claim_id,
        "review_claim_payload_sha256": review_claim_payload_sha256,
        "lane": lane,
        "decision": decision,
        "review_runner_canonical_path_sha256": str(
            getattr(claim, "review_runner_canonical_path_sha256")
        ),
        "review_runner_binary_sha256": str(
            getattr(claim, "review_runner_binary_sha256")
        ),
        "prompt_sha256": f"sha256:{hashlib.sha256(prompt.encode('utf-8')).hexdigest()}",
        "stdout": stdout,
        "stderr": "",
        "returncode": 0,
    })


def _source_identity(
        partition: AV1ValidationPrivatePartition,
        assignment: AV1ValidationPartitionAssignment,
) -> str:
    return next(
        source.source_identity
        for source in partition.inventory_sources
        if source.local_item_id == assignment.local_item_id
    )


def _partition_sources(
        expectations: AV1ValidationPartitionExpectations,
) -> tuple[AV1ValidationPartitionSource, ...]:
    sources = []
    local_item_id = 1
    for label, traits, count in (
        ("darkness", ("darkness",), 32),
        ("motion", ("motion",), 32),
        ("grain", ("grain_noise",), 6),
        ("mixed", ("mixed",), 6),
        ("texture", ("texture_detail",), 6),
        ("typical", ("typical",), 12),
    ):
        for ordinal in range(1, count + 1):
            identity = f"{label}-{ordinal:03d}"
            sources.append(
                AV1ValidationPartitionSource(
                    local_item_id=local_item_id,
                    source_identity=f"source-{identity}",
                    title_identity=f"title-{identity}",
                    series_identity=f"series-{identity}",
                    source_group_identity=f"group-{identity}",
                    traits=traits,
                    compatibility_signature=expectations.compatibility_signature,
                    base_policy_signature=expectations.base_policy_signature,
                    target_video_bitrate_bps=500_000 + local_item_id,
                    quality_metric=expectations.quality_metric,
                    quality_target=expectations.quality_target,
                    minimum_quality_score=expectations.minimum_quality_score,
                    evidence_summary_sha256=f"sha256:{local_item_id:064x}",
                )
            )
            local_item_id += 1
    return tuple(sources)


if __name__ == "__main__":
    unittest.main()
