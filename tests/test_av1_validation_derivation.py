from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext, redirect_stdout
from dataclasses import replace
import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Callable, Literal, Sequence
import unittest
from unittest.mock import call, patch

from sqlalchemy.exc import SQLAlchemyError

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.core.db import open_db
from mediaforce.core.file_integrity import FileIntegrityError, MacOSFileIntegrityGuard
from mediaforce.core.process_control import (
    ManagedProcessController,
    ProcessCancelledError,
    ProcessDeadlineEnforcementError,
    ProcessDeadlineExpiredError,
)
from mediaforce.core.utils import content_version_fingerprint
from mediaforce.tuning.av1_validation_derivation import (
    AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY,
    AV1_VALIDATION_DERIVATION_REVIEW_LANES,
    AV1_VALIDATION_DERIVATION_PERSONALIZATION_EXCLUSION_REASON,
    AV1ValidationDerivationAttempt,
    AV1ValidationDerivationCandidateProposal,
    AV1ValidationDerivationError,
    AV1ValidationDerivationPlan,
    AV1ValidationDerivationPublicationDeadlineError,
    AV1ValidationDerivationReviewClaim,
    AV1ValidationDerivationSourceCommitment,
    AV1ValidationDerivationTerminalRecord,
    AV1ValidationDerivationVerdictRetryMismatchError,
    _attempt_semantic_payload,
    _bind_owner_only_directory,
    _code_review_marker,
    _completed_code_review_message,
    _derivation_id,
    _payload_sha256,
    _read_owner_only_bytes,
    _rename_owner_only_exclusive,
    _av1_validation_derivation_review_set_sha256,
    _terminal_semantic_payload,
    _write_owner_only,
    assert_av1_validation_derivation_authorization_active,
    assert_av1_validation_derivation_source_commitments,
    av1_validation_derivation_plan_public_summary,
    av1_validation_derivation_plan_from_payload,
    av1_validation_derivation_plan_source_commitment,
    av1_validation_derivation_source_commitment_sha256,
    av1_validation_derivation_statistics_contract_sha256,
    build_av1_validation_derivation_attempt,
    build_av1_validation_derivation_plan,
    build_av1_validation_derivation_source_commitments,
    build_av1_validation_derivation_review_claim,
    build_av1_validation_derivation_review_prompt,
    build_av1_validation_derivation_review_attestation,
    build_av1_validation_derivation_review_envelope,
    build_av1_validation_derivation_terminal_record,
    ensure_av1_validation_derivation_verdict_claim,
    ensure_av1_validation_derivation_terminal_intent,
    ensure_av1_validation_derivation_terminal_record,
    evaluate_av1_validation_derivation_candidate,
    _finalize_and_write_av1_validation_derivation_candidate_lock as finalize_and_write_av1_validation_derivation_candidate_lock,
    finalize_av1_validation_derivation_candidate_lock,
    load_av1_validation_derivation_candidate_proposal,
    load_av1_validation_derivation_assignment_claims,
    _load_verified_av1_validation_derivation_candidate_lock as load_verified_av1_validation_derivation_candidate_lock,
    load_av1_validation_derivation_plan,
    load_av1_validation_derivation_attempts,
    load_av1_validation_derivation_review_claims,
    load_av1_validation_derivation_review_envelope,
    load_av1_validation_derivation_review_envelopes,
    load_av1_validation_derivation_terminal_records,
    resolve_av1_validation_derivation_verdict_intent,
    retain_av1_validation_derivation_publication_directories,
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
    _AV1ValidationDerivationVerdictSafetyStop,
    _assert_next_assignment,
    _assert_derivation_terminal_observations_current,
    _current_derivation_review_artifact_fingerprint,
    _av1_validation_derivation_candidate_locked_at,
    _av1_validation_derivation_implementation_files,
    _owner_only_umask,
    _prepare_derivation_review_root,
    _pinned_derivation_source,
    _recover_interrupted_derivation_state,
    _run_av1_validation_derivation_assignment_locked,
    _secure_derivation_review_media,
    _validated_av1_validation_derivation_artifact_root,
    _write_all,
    assert_av1_validation_derivation_execution_contract,
    assert_av1_validation_derivation_execution_environment,
    av1_validation_derivation_execution_environment_sha256,
    av1_validation_derivation_runtime_context_sha256,
    finalize_av1_validation_derivation_candidate_lock as finalize_runtime_av1_validation_derivation_candidate_lock,
    load_verified_av1_validation_derivation_candidate_lock as load_verified_runtime_av1_validation_derivation_candidate_lock,
    record_av1_validation_derivation_visual_verdict,
    run_av1_validation_derivation_assignment,
)
from mediaforce.web.runtime_lock import (
    MediaforceRuntimeBusyError,
    MediaforceRuntimeLease,
    MediaforceRuntimeLockOwnershipError,
    exclusive_mediaforce_runtime_lock,
    mediaforce_runtime_lock_path,
    reserve_mediaforce_database_identity,
)
from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1ColdStartValidationError,
    build_av1_cold_start_validation_candidate_lock,
)
from mediaforce.tuning.av1_validation_partition import (
    AV1ValidationPartitionError,
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
REVIEW_REPOSITORY_COMMIT = "1" * 40
REVIEW_REPOSITORY_TREE = "2" * 40
SOURCE_SIZE_BYTES = 900_000_000


def _source_sha256(source: AV1ValidationPartitionSource) -> str:
    return _source_sha256_for_identity(source.source_identity)


def _source_sha256_for_identity(source_identity: str) -> str:
    return f"sha256:{hashlib.sha256(source_identity.encode()).hexdigest()}"


class _SourceSHA256Session:
    def __init__(
            self,
            on_verify: Callable[[], None] | None = None,
            on_quiet: Callable[[], None] | None = None,
    ) -> None:
        self._on_verify = on_verify
        self._on_quiet = on_quiet

    def __call__(self, source: AV1ValidationPartitionSource) -> str:
        return _source_sha256(source)

    def source_size_bytes(self, source: AV1ValidationPartitionSource) -> int:
        del source
        return SOURCE_SIZE_BYTES

    def verify(self) -> None:
        if self._on_verify is not None:
            self._on_verify()

    def assert_quiet(self) -> None:
        if self._on_quiet is not None:
            self._on_quiet()


@contextmanager
def _source_sha256_resolver_context(
        *_args: object,
        **_kwargs: object,
) -> Iterator[object]:
    session = _SourceSHA256Session()
    yield session
    session.assert_quiet()


@contextmanager
def _context_value(value: object) -> Iterator[object]:
    yield value


def _run_test_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repository),
            "-c",
            "user.name=Mediaforce Test",
            "-c",
            "user.email=mediaforce-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(repository.parent),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        },
    )
    return completed.stdout.strip()


class _DescriptorBindingFileIntegrityGuard:
    def __init__(
            self,
            *,
            path: Path,
            descriptor: int,
            require_single_link: bool,
    ) -> None:
        self.path = path.expanduser().resolve(strict=True)
        self._descriptor = descriptor
        self._require_single_link = require_single_link
        self.assert_quiet()

    def assert_quiet(self, *, timeout_seconds: float = 0.0) -> None:
        descriptor_info = os.fstat(self._descriptor)
        path_info = self.path.lstat()
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
            or (
                self._require_single_link
                and (descriptor_info.st_nlink != 1 or path_info.st_nlink != 1)
            )
        ):
            raise FileIntegrityError("guarded file or path changed")

    def close(self) -> None:
        pass


class AV1ValidationDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_directory = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_directory.cleanup)
        runtime_root = Path(runtime_directory.name)
        self.runtime_config = SimpleNamespace(
            paths=SimpleNamespace(
                config_path=runtime_root / "config.toml",
                db_path=runtime_root / "mediaforce.sqlite3",
                review_dir=runtime_root / "review",
                web_state_dir=runtime_root / "state",
            )
        )
        self.runtime_config.paths.db_path.touch(mode=0o600)
        runtime_lock = exclusive_mediaforce_runtime_lock(
            self.runtime_config,
            owner_payload={"purpose": "derivation-test"},
        )
        self.runtime_lease = runtime_lock.__enter__()
        self.addCleanup(runtime_lock.__exit__, None, None, None)
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
        source_resolver_patcher = patch(
            "mediaforce.web.runtime.av1_validation_derivation.av1_validation_partition_source_sha256_resolver",
            side_effect=_source_sha256_resolver_context,
        )
        source_resolver_patcher.start()
        self.addCleanup(source_resolver_patcher.stop)
        for migration_patcher in (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.migrate_config_state",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.reserve_mediaforce_database_identity",
            ),
            patch(
                "scripts.verify_av1_cold_start_preregistration.migrate_config_state",
            ),
            patch(
                "scripts.verify_av1_cold_start_preregistration.reserve_mediaforce_database_identity",
            ),
        ):
            migration_patcher.start()
            self.addCleanup(migration_patcher.stop)
        for integrity_patcher in (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_macos_file_integrity_capability",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.probe_macos_file_integrity",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._available_bytes_for_descriptor",
                return_value=100 * 1024 ** 3,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.MacOSFileIntegrityGuard",
                new=_DescriptorBindingFileIntegrityGuard,
            ),
        ):
            integrity_patcher.start()
            self.addCleanup(integrity_patcher.stop)
        self.publication_time_patcher = patch(
            "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
            return_value=0,
        )
        self.publication_time_patcher.start()
        self.addCleanup(self.publication_time_patcher.stop)
        if not hasattr(__import__("select"), "kqueue"):
            review_guard_patcher = patch.object(
                verify_av1_cold_start_preregistration,
                "MacOSFileIntegrityGuard",
                new=_DescriptorBindingFileIntegrityGuard,
            )
            review_guard_patcher.start()
            self.addCleanup(review_guard_patcher.stop)
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
            authorized_at=AUTHORIZED_AT,
            valid_until=VALID_UNTIL,
        )
        self.source_commitments = (
            build_av1_validation_derivation_source_commitments(
                partition=self.partition,
                assignments=tuple(
                    assignment
                    for assignment in self.partition.assignments
                    if assignment.role == "derivation"
                ),
                resolver=_SourceSHA256Session(),
            )
        )
        self.plan = build_av1_validation_derivation_plan(
            manifest=self.manifest,
            partition=self.partition,
            authorization=self.authorization,
            runtime_context_sha256=runtime_context_sha256,
            execution_environment_sha256=execution_environment_sha256,
            statistics_contract_sha256=(
                av1_validation_derivation_statistics_contract_sha256(self.manifest)
            ),
            review_runner_canonical_path_sha256=f"sha256:{'a' * 64}",
            review_runner_binary_sha256=(
                f"sha256:{hashlib.sha256(REVIEW_RUNNER_BYTES).hexdigest()}"
            ),
            repository_commit=REVIEW_REPOSITORY_COMMIT,
            repository_tree=REVIEW_REPOSITORY_TREE,
            source_commitments=self.source_commitments,
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

    def _retained_snapshot_path(self, artifact_root: Path) -> Path:
        assignment_id = self.plan.assignments[0].assignment_id
        return (
            artifact_root
            / "source-snapshots"
            / f"{assignment_id}.source-media"
        )

    def _review_pending_attempt(self) -> AV1ValidationDerivationAttempt:
        assignment = self.plan.assignments[0]
        return build_av1_validation_derivation_attempt(
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

    def _matching_repository_identity(self) -> tuple[str, str]:
        return self.plan.repository_commit, self.plan.repository_tree

    def _run_assignment_with_repository_drift(
            self,
            *,
            drift_phase: Literal["before_media", "before_publication"],
    ) -> bool:
        assignment = self.plan.assignments[0]
        source = next(
            item
            for item in self.partition.inventory_sources
            if item.local_item_id == assignment.local_item_id
        )
        attempts_directory = self.runtime_artifact_root / "attempts"
        terminal_records_directory = self.runtime_artifact_root / "terminal-records"
        source_commitment = av1_validation_derivation_plan_source_commitment(
            self.plan,
            assignment.assignment_id,
        )
        pinned_source = SimpleNamespace(
            path=self.runtime_artifact_root / "source-snapshots" / "source.mkv",
            content_sha256=source_commitment.source_sha256,
            size_bytes=source_commitment.source_size_bytes,
            content_version_fingerprint=source.source_identity,
        )
        sample_item = {
            "library_item_id": assignment.local_item_id,
            "source_size_bytes": SOURCE_SIZE_BYTES,
            "resolved_policy": {},
        }
        calibration_payload = self._review_pending_attempt().calibration_payload()
        live_identity = [self.plan.repository_commit, self.plan.repository_tree]
        drifted_identity = ["b" * 40, "c" * 40]
        calibration_ran = False

        def repository_identity_resolver() -> tuple[str, str]:
            return live_identity[0], live_identity[1]

        @contextmanager
        def pinned_source_context(**_kwargs: object) -> Iterator[object]:
            if drift_phase == "before_media":
                live_identity[:] = drifted_identity
            yield pinned_source

        def run_calibration(**_kwargs: object) -> tuple[dict[str, object], None]:
            nonlocal calibration_ran
            calibration_ran = True
            return dict(calibration_payload), None

        def build_attempt_with_drift(**kwargs: object) -> AV1ValidationDerivationAttempt:
            attempt = build_av1_validation_derivation_attempt(**kwargs)
            live_identity[:] = drifted_identity
            return attempt

        attempt_builder = (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.build_av1_validation_derivation_attempt",
                side_effect=build_attempt_with_drift,
            )
            if drift_phase == "before_publication"
            else nullcontext()
        )
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_readonly_db",
                return_value=nullcontext(object()),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory",
                return_value=SimpleNamespace(
                    sources=self.sources,
                    expectations=self.expectations,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                return_value=SimpleNamespace(free=100 * 1024 ** 3),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.purge_transient_artifacts",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_db",
                return_value=nullcontext(SimpleNamespace()),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_sample_item",
                return_value=sample_item,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._bind_derivation_intent",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._validate_bound_sample_item",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.snapshot_staged_artifact",
                return_value=None,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.restore_staged_artifact",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.resolve_item_source_path",
                return_value=Path("/private/source.mkv"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._derivation_prefix",
                return_value="private/derivation",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._pinned_derivation_source",
                side_effect=pinned_source_context,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.run_sampled_calibration",
                side_effect=run_calibration,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                return_value=calibration_payload["review_artifact_fingerprint"],
            ),
            attempt_builder,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "repository snapshot drifted",
            ),
        ):
            _run_av1_validation_derivation_assignment_locked(
                config=self.runtime_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=repository_identity_resolver,
                assignment_id=assignment.assignment_id,
                attempts_directory=attempts_directory,
                terminal_records_directory=terminal_records_directory,
                now_iso=lambda: "2026-07-30T01:00:00Z",
            )
        self.assertEqual(list(attempts_directory.glob("*.json")), [])
        self.assertFalse(terminal_records_directory.exists())
        return calibration_ran

    def _cross_domain_artifact_alias(self) -> tuple[SimpleNamespace, Path]:
        alias_state_root = (
            self.runtime_config.paths.web_state_dir.parent
            / "alias-runtime"
            / "alias-state"
        )
        alias_state_root.mkdir(mode=0o700, parents=True)
        (
            alias_state_root / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
        ).symlink_to(
            self.runtime_artifact_root.parent,
            target_is_directory=True,
        )
        alias_config = SimpleNamespace(
            paths=SimpleNamespace(
                db_path=self.runtime_config.paths.db_path,
                review_dir=self.runtime_config.paths.review_dir,
                web_state_dir=alias_state_root,
            )
        )
        return (
            alias_config,
            alias_state_root
            / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
            / self.plan.partition_id,
        )

    def test_execution_environment_rejects_unavailable_source_integrity(self) -> None:
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_macos_file_integrity_capability",
                side_effect=FileIntegrityError("fixture unavailable"),
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "source-integrity monitoring is unavailable",
            ),
        ):
            av1_validation_derivation_execution_environment_sha256(
                quality_metric=self.expectations.quality_metric,
            )

    def test_plan_contains_only_exact_reserved_derivation_assignments(self) -> None:
        self.assertEqual(len(self.plan.assignments), 24)
        self.assertEqual(len(self.plan.source_commitments), 24)
        self.assertEqual(
            {commitment.assignment_id for commitment in self.plan.source_commitments},
            {assignment.assignment_id for assignment in self.plan.assignments},
        )
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
        self.assertTrue(summary["repository_snapshot_bound"])

    def test_plan_identity_binds_source_commitments(self) -> None:
        changed_item_id = self.plan.assignments[0].local_item_id

        class ChangedSourceSession(_SourceSHA256Session):
            def __call__(self, source: AV1ValidationPartitionSource) -> str:
                if source.local_item_id == changed_item_id:
                    return f"sha256:{'f' * 64}"
                return super().__call__(source)

        commitments = build_av1_validation_derivation_source_commitments(
            partition=self.partition,
            assignments=self.plan.assignments,
            resolver=ChangedSourceSession(),
        )
        drifted_plan = build_av1_validation_derivation_plan(
            manifest=self.manifest,
            partition=self.partition,
            authorization=self.authorization,
            runtime_context_sha256=self.plan.runtime_context_sha256,
            execution_environment_sha256=self.plan.execution_environment_sha256,
            statistics_contract_sha256=self.plan.statistics_contract_sha256,
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=self.plan.review_runner_binary_sha256,
            repository_commit=self.plan.repository_commit,
            repository_tree=self.plan.repository_tree,
            source_commitments=commitments,
        )
        self.assertNotEqual(drifted_plan.plan_id, self.plan.plan_id)
        self.assertNotEqual(
            drifted_plan.source_commitment_sha256,
            self.plan.source_commitment_sha256,
        )

    def test_source_commitment_payload_keys_are_frozen(self) -> None:
        self.assertEqual(
            set(self.plan.source_commitments[0].to_payload()),
            {
                "schema",
                "schema_version",
                "contract_version",
                "assignment_id",
                "local_item_id",
                "source_identity",
                "source_sha256",
                "source_size_bytes",
                "evidence_summary_sha256",
            },
        )

    def test_plan_rejects_missing_or_extra_source_commitment(self) -> None:
        for commitments in (
            self.plan.source_commitments[:-1],
            (*self.plan.source_commitments, self.plan.source_commitments[-1]),
        ):
            with self.subTest(commitment_count=len(commitments)), self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "source commitments are incomplete",
            ):
                build_av1_validation_derivation_plan(
                    manifest=self.manifest,
                    partition=self.partition,
                    authorization=self.authorization,
                    runtime_context_sha256=self.plan.runtime_context_sha256,
                    execution_environment_sha256=(
                        self.plan.execution_environment_sha256
                    ),
                    statistics_contract_sha256=(
                        self.plan.statistics_contract_sha256
                    ),
                    review_runner_canonical_path_sha256=(
                        self.plan.review_runner_canonical_path_sha256
                    ),
                    review_runner_binary_sha256=(
                        self.plan.review_runner_binary_sha256
                    ),
                    repository_commit=self.plan.repository_commit,
                    repository_tree=self.plan.repository_tree,
                    source_commitments=commitments,
                )

    def test_plan_rejects_commitment_for_unknown_assignment(self) -> None:
        commitments = list(self.plan.source_commitments)
        commitments[0] = replace(
            commitments[0],
            assignment_id="unknown_assignment_0001",
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "do not cover its assignments",
        ):
            build_av1_validation_derivation_plan(
                manifest=self.manifest,
                partition=self.partition,
                authorization=self.authorization,
                runtime_context_sha256=self.plan.runtime_context_sha256,
                execution_environment_sha256=self.plan.execution_environment_sha256,
                statistics_contract_sha256=self.plan.statistics_contract_sha256,
                review_runner_canonical_path_sha256=(
                    self.plan.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=self.plan.review_runner_binary_sha256,
                repository_commit=self.plan.repository_commit,
                repository_tree=self.plan.repository_tree,
                source_commitments=commitments,
            )

    def test_plan_rejects_commitment_for_unknown_source_identity(self) -> None:
        commitments = list(self.plan.source_commitments)
        commitments[0] = replace(
            commitments[0],
            source_identity="drifted-source-identity",
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "does not match its partition source",
        ):
            build_av1_validation_derivation_plan(
                manifest=self.manifest,
                partition=self.partition,
                authorization=self.authorization,
                runtime_context_sha256=self.plan.runtime_context_sha256,
                execution_environment_sha256=self.plan.execution_environment_sha256,
                statistics_contract_sha256=self.plan.statistics_contract_sha256,
                review_runner_canonical_path_sha256=(
                    self.plan.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.plan.review_runner_binary_sha256
                ),
                repository_commit=self.plan.repository_commit,
                repository_tree=self.plan.repository_tree,
                source_commitments=commitments,
            )

    def test_live_source_byte_drift_fails_before_plan_publication(self) -> None:
        changed_item_id = self.plan.assignments[0].local_item_id

        class ChangedSourceSession(_SourceSHA256Session):
            def __call__(self, source: AV1ValidationPartitionSource) -> str:
                if source.local_item_id == changed_item_id:
                    return f"sha256:{'f' * 64}"
                return super().__call__(source)

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "drifted from the immutable plan",
        ):
            assert_av1_validation_derivation_source_commitments(
                self.plan,
                resolver=ChangedSourceSession(),
            )

    def test_live_source_size_drift_fails_before_plan_publication(self) -> None:
        changed_item_id = self.plan.assignments[0].local_item_id

        class ChangedSourceSession(_SourceSHA256Session):
            def source_size_bytes(
                    self,
                    source: AV1ValidationPartitionSource,
            ) -> int:
                if source.local_item_id == changed_item_id:
                    return SOURCE_SIZE_BYTES + 1
                return super().source_size_bytes(source)

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "drifted from the immutable plan",
        ):
            assert_av1_validation_derivation_source_commitments(
                self.plan,
                resolver=ChangedSourceSession(),
            )

    def test_plan_retry_reproduces_frozen_commitments(self) -> None:
        rebuilt = build_av1_validation_derivation_plan(
            manifest=self.manifest,
            partition=self.partition,
            authorization=self.authorization,
            runtime_context_sha256=self.plan.runtime_context_sha256,
            execution_environment_sha256=self.plan.execution_environment_sha256,
            statistics_contract_sha256=self.plan.statistics_contract_sha256,
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=self.plan.review_runner_binary_sha256,
            repository_commit=self.plan.repository_commit,
            repository_tree=self.plan.repository_tree,
            source_commitments=build_av1_validation_derivation_source_commitments(
                partition=self.partition,
                assignments=self.plan.assignments,
                resolver=_SourceSHA256Session(),
            ),
        )
        self.assertEqual(rebuilt, self.plan)

    def test_plan_binds_execution_environment_and_runner_digests(self) -> None:
        changed_environment = build_av1_validation_derivation_plan(
            manifest=self.manifest,
            partition=self.partition,
            authorization=self.authorization,
            runtime_context_sha256=self.plan.runtime_context_sha256,
            execution_environment_sha256=f"sha256:{'e' * 64}",
            statistics_contract_sha256=self.plan.statistics_contract_sha256,
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=self.plan.review_runner_binary_sha256,
            repository_commit=self.plan.repository_commit,
            repository_tree=self.plan.repository_tree,
            source_commitments=self.plan.source_commitments,
        )
        changed_runner = build_av1_validation_derivation_plan(
            manifest=self.manifest,
            partition=self.partition,
            authorization=self.authorization,
            runtime_context_sha256=self.plan.runtime_context_sha256,
            execution_environment_sha256=self.plan.execution_environment_sha256,
            statistics_contract_sha256=self.plan.statistics_contract_sha256,
            review_runner_canonical_path_sha256=f"sha256:{'f' * 64}",
            review_runner_binary_sha256=f"sha256:{'0' * 64}",
            repository_commit=self.plan.repository_commit,
            repository_tree=self.plan.repository_tree,
            source_commitments=self.plan.source_commitments,
        )
        self.assertNotEqual(changed_environment.plan_id, self.plan.plan_id)
        self.assertNotEqual(changed_runner.plan_id, self.plan.plan_id)

        changed_repository = build_av1_validation_derivation_plan(
            manifest=self.manifest,
            partition=self.partition,
            authorization=self.authorization,
            runtime_context_sha256=self.plan.runtime_context_sha256,
            execution_environment_sha256=self.plan.execution_environment_sha256,
            statistics_contract_sha256=self.plan.statistics_contract_sha256,
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=self.plan.review_runner_binary_sha256,
            repository_commit="3" * 40,
            repository_tree="4" * 40,
            source_commitments=self.plan.source_commitments,
        )
        self.assertNotEqual(changed_repository.plan_id, self.plan.plan_id)

    def test_v2_plan_parsing_fails_closed_without_repository_identity(self) -> None:
        for missing_key in ("repository_commit", "repository_tree"):
            with self.subTest(missing_key=missing_key):
                payload = self.plan.to_payload()
                del payload[missing_key]
                with self.assertRaises(AV1ValidationDerivationError):
                    av1_validation_derivation_plan_from_payload(payload)

    def test_implementation_drift_invalidates_execution_environment(self) -> None:
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._av1_validation_derivation_implementation_sha256",
                return_value=f"sha256:{'f' * 64}",
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "execution environment drifted",
            ),
        ):
            assert_av1_validation_derivation_execution_environment(self.plan)

    def test_execution_environment_probe_runs_under_authorization_deadline(self) -> None:
        controller = ManagedProcessController()

        def probe_environment(
                *,
                quality_metric: str,
                process_controller: ManagedProcessController,
        ) -> str:
            self.assertEqual(quality_metric, self.expectations.quality_metric)
            self.assertIs(process_controller, controller)
            self.assertIsNotNone(process_controller.process_deadline_ns())
            return self.plan.execution_environment_sha256

        with patch(
            "mediaforce.web.runtime.av1_validation_derivation.av1_validation_derivation_execution_environment_sha256",
            side_effect=probe_environment,
        ):
            assert_av1_validation_derivation_execution_environment(
                self.plan,
                process_controller=controller,
            )
        self.assertIsNone(controller.process_deadline_ns())

    def test_execution_environment_preserves_operator_stop_classification(self) -> None:
        operator_stop = ProcessCancelledError("operator requested stop")

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.av1_validation_derivation_execution_environment_sha256",
                side_effect=operator_stop,
            ),
            self.assertRaises(ProcessCancelledError) as raised,
        ):
            assert_av1_validation_derivation_execution_environment(self.plan)

        self.assertIs(raised.exception, operator_stop)

    def test_execution_environment_preserves_deadline_expiry_classification(self) -> None:
        deadline_expired = ProcessDeadlineExpiredError(
            "authorization deadline expired"
        )

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.av1_validation_derivation_execution_environment_sha256",
                side_effect=deadline_expired,
            ),
            self.assertRaises(ProcessDeadlineExpiredError) as raised,
        ):
            assert_av1_validation_derivation_execution_environment(self.plan)

        self.assertIs(raised.exception, deadline_expired)

    def test_execution_environment_wraps_deadline_enforcement_failure(self) -> None:
        enforcement_failed = ProcessDeadlineEnforcementError(
            "deadline watchdog failed"
        )

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.av1_validation_derivation_execution_environment_sha256",
                side_effect=enforcement_failed,
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "execution environment could not be verified",
            ) as raised,
        ):
            assert_av1_validation_derivation_execution_environment(self.plan)

        self.assertIs(raised.exception.__cause__, enforcement_failed)

    def test_implementation_identity_covers_complete_runtime_tree(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        relative_paths = {
            relative_path
            for relative_path, _path in (
                _av1_validation_derivation_implementation_files(repository_root)
            )
        }
        self.assertTrue({
            "mediaforce/execution.py",
            "mediaforce/encoding/quality.py",
            "mediaforce/review.py",
            "mediaforce/tuning/av1_cold_start_evaluation.py",
            "mediaforce/tuning/content_intent_observations.py",
            "pyproject.toml",
            "scripts/verify_av1_cold_start_preregistration.py",
            "uv.lock",
        }.issubset(relative_paths))
        self.assertFalse(any("__pycache__" in path for path in relative_paths))
        self.assertFalse(any(path.endswith((".pyc", ".pyo")) for path in relative_paths))

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
        source_identity = next(
            source.source_identity
            for source in self.partition.inventory_sources
            if source.local_item_id == assignments[0].local_item_id
        )
        source_commitments = [
            replace(
                commitment,
                local_item_id=assignments[0].local_item_id,
                source_identity=source_identity,
            )
            if commitment.assignment_id == assignments[0].assignment_id
            else commitment
            for commitment in self.plan.source_commitments
        ]
        source_commitments = sorted(
            source_commitments,
            key=lambda commitment: commitment.assignment_id,
        )
        source_commitment_sha256 = (
            av1_validation_derivation_source_commitment_sha256(
                source_commitments
            )
        )
        semantic_payload["source_commitments"] = [
            commitment.to_payload()
            for commitment in source_commitments
        ]
        semantic_payload["source_commitment_sha256"] = (
            source_commitment_sha256
        )
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
            execution_environment_sha256=self.plan.execution_environment_sha256,
            statistics_contract_sha256=self.plan.statistics_contract_sha256,
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=self.plan.review_runner_binary_sha256,
            repository_commit=self.plan.repository_commit,
            repository_tree=self.plan.repository_tree,
            authorization=self.plan.authorization,
            assignments=tuple(assignments),
            source_commitments=tuple(source_commitments),
            source_commitment_sha256=source_commitment_sha256,
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
            self.assertEqual(os.stat(plan_path).st_mode & 0o777, 0o400)
            self.assertEqual(load_av1_validation_derivation_plan(plan_path), self.plan)
            plan_path.chmod(0o600)
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "immutable and owner-only",
            ):
                load_av1_validation_derivation_plan(plan_path)
            plan_path.chmod(0o400)
            hardlink_path = root / "plan-hardlink.json"
            os.link(plan_path, hardlink_path)
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "immutable and owner-only",
            ):
                load_av1_validation_derivation_plan(plan_path)
            hardlink_path.unlink()
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
            write_av1_validation_derivation_attempt(attempts_dir, attempt)

            records_dir = root / "records"
            write_av1_validation_derivation_terminal_record(records_dir, record)
            self.assertEqual(load_av1_validation_derivation_terminal_records(records_dir), (record,))
            write_av1_validation_derivation_terminal_record(records_dir, record)

    def test_artifact_root_rejects_final_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_parent = root / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
            artifact_parent.mkdir(mode=0o700)
            outside = root / "outside"
            outside.mkdir(mode=0o700)
            artifact_root = artifact_parent / self.plan.partition_id
            artifact_root.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "created safely|directory is unsafe",
            ):
                write_av1_validation_derivation_plan(
                    artifact_root,
                    self.plan,
                )

    def test_relocated_state_root_is_rejected_before_recovery_writes(self) -> None:
        assignment = self.plan.assignments[0]
        attempts_directory = self.runtime_artifact_root / "attempts"
        write_av1_validation_derivation_assignment_claim(
            attempts_directory,
            assignment_id=assignment.assignment_id,
            plan_id=self.plan.plan_id,
            authorization_id=self.plan.authorization.authorization_id,
            claimed_at="2026-07-28T01:00:00Z",
        )
        relocated_state_root = (
            self.runtime_config.paths.web_state_dir.parent / "relocated-state"
        )
        relocated_state_root.mkdir(mode=0o700)
        shutil.move(
            self.runtime_artifact_root.parent,
            relocated_state_root / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY,
        )
        relocated_artifact_root = (
            relocated_state_root
            / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
            / self.plan.partition_id
        )
        relocated_config = SimpleNamespace(
            paths=SimpleNamespace(
                db_path=self.runtime_config.paths.db_path,
                review_dir=self.runtime_config.paths.review_dir,
                web_state_dir=relocated_state_root,
            )
        )

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "artifact-root binding drifted",
        ):
            _run_av1_validation_derivation_assignment_locked(
                config=relocated_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=self._matching_repository_identity,
                assignment_id=assignment.assignment_id,
                attempts_directory=relocated_artifact_root / "attempts",
                terminal_records_directory=(
                    relocated_artifact_root / "terminal-records"
                ),
            )
        self.assertEqual(
            list((relocated_artifact_root / "attempts").glob("*.json")),
            [],
        )
        self.assertFalse((relocated_artifact_root / "terminal-records").exists())

    def test_symlinked_artifact_tree_is_rejected_before_recovery_writes(self) -> None:
        assignment = self.plan.assignments[0]
        attempts_directory = self.runtime_artifact_root / "attempts"
        write_av1_validation_derivation_assignment_claim(
            attempts_directory,
            assignment_id=assignment.assignment_id,
            plan_id=self.plan.plan_id,
            authorization_id=self.plan.authorization.authorization_id,
            claimed_at="2026-07-28T01:00:00Z",
        )
        alias_config, aliased_artifact_root = self._cross_domain_artifact_alias()

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "runtime-lock domain drifted",
        ):
            _run_av1_validation_derivation_assignment_locked(
                config=alias_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=self._matching_repository_identity,
                assignment_id=assignment.assignment_id,
                attempts_directory=aliased_artifact_root / "attempts",
                terminal_records_directory=(
                    aliased_artifact_root / "terminal-records"
                ),
            )
        self.assertEqual(
            list((self.runtime_artifact_root / "attempts").glob("*.json")),
            [],
        )
        self.assertFalse((self.runtime_artifact_root / "terminal-records").exists())

    def test_candidate_lock_apis_reject_cross_domain_artifact_alias(self) -> None:
        alias_config, aliased_artifact_root = self._cross_domain_artifact_alias()
        for operation in (
            finalize_runtime_av1_validation_derivation_candidate_lock,
            load_verified_runtime_av1_validation_derivation_candidate_lock,
        ):
            with self.subTest(operation=operation.__name__):
                with (
                    patch(
                        "mediaforce.web.runtime.av1_validation_derivation.load_config",
                        return_value=alias_config,
                    ),
                    patch(
                        "mediaforce.web.runtime.av1_validation_derivation._load_canonical_av1_validation_derivation_plan",
                        return_value=(self.plan, aliased_artifact_root),
                    ),
                    patch(
                        "mediaforce.web.runtime.av1_validation_derivation.exclusive_mediaforce_runtime_lock",
                        return_value=nullcontext(),
                    ),
                    patch(
                        "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_candidate_proposal",
                    ) as load_proposal,
                    self.assertRaisesRegex(
                        AV1ValidationDerivationError,
                        "runtime-lock domain drifted",
                    ),
                ):
                    operation(
                        config_path=Path("unused.toml"),
                        manifest=self.manifest,
                        partition=self.partition,
                        token_key=self.token_key,
                        plan_path=aliased_artifact_root / "plan.json",
                        cell_plan_id=self.plan.assignments[0].cell_plan_id,
                        repository_commit=self.plan.repository_commit,
                        repository_tree=self.plan.repository_tree,
                    )
                load_proposal.assert_not_called()
        self.assertFalse((self.runtime_artifact_root / "candidate-locks").exists())

    def test_visual_verdict_rejects_cross_domain_alias_before_claim(self) -> None:
        alias_config, aliased_artifact_root = self._cross_domain_artifact_alias()
        attempt = self._review_pending_attempt()
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_config",
                return_value=alias_config,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.exclusive_mediaforce_runtime_lock",
                return_value=nullcontext(),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.av1_validation_derivation_artifact_root",
                return_value=aliased_artifact_root,
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "runtime-lock domain drifted",
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
                    aliased_artifact_root / "terminal-records"
                ),
                verdict="approved",
                concern_tags=[],
                evidence_ids=[],
                moment_indexes=[],
                recorded_at="2026-07-28T01:06:00Z",
            )
        for directory_name in (
            "verdict-claims",
            "verdict-intents",
            "terminal-intents",
            "terminal-records",
        ):
            self.assertFalse((self.runtime_artifact_root / directory_name).exists())

    def test_visual_verdict_safety_fallback_rechecks_lock_domain(self) -> None:
        alias_config, aliased_artifact_root = self._cross_domain_artifact_alias()
        attempt = self._review_pending_attempt()
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_config",
                return_value=alias_config,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.exclusive_mediaforce_runtime_lock",
                return_value=nullcontext(),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._record_av1_validation_derivation_visual_verdict_locked",
                side_effect=_AV1ValidationDerivationVerdictSafetyStop(
                    "fixture safety stop"
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.av1_validation_derivation_artifact_root",
                return_value=aliased_artifact_root,
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "runtime-lock domain drifted",
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
                    aliased_artifact_root / "terminal-records"
                ),
                verdict="approved",
                concern_tags=[],
                evidence_ids=[],
                moment_indexes=[],
                recorded_at="2026-07-28T01:06:00Z",
            )
        self.assertFalse((self.runtime_artifact_root / "terminal-intents").exists())
        self.assertFalse((self.runtime_artifact_root / "terminal-records").exists())

    def test_same_domain_web_state_alias_uses_canonical_artifact_root(self) -> None:
        alias_state_root = (
            self.runtime_config.paths.web_state_dir.parent / "state-alias"
        )
        alias_state_root.symlink_to(
            self.runtime_config.paths.web_state_dir,
            target_is_directory=True,
        )
        alias_config = SimpleNamespace(
            paths=SimpleNamespace(
                db_path=self.runtime_config.paths.db_path,
                review_dir=self.runtime_config.paths.review_dir,
                web_state_dir=alias_state_root,
            )
        )
        self.assertEqual(
            _validated_av1_validation_derivation_artifact_root(
                config=alias_config,
                plan=self.plan,
                artifact_root=(
                    alias_state_root
                    / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
                    / self.plan.partition_id
                ),
            ),
            self.runtime_artifact_root.resolve(),
        )

    def test_runtime_artifact_root_rejects_repository_containment(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        web_state_dir = repository_root / ".private-av1-state"
        artifact_root = (
            web_state_dir
            / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
            / self.plan.partition_id
        )
        config = SimpleNamespace(
            paths=SimpleNamespace(
                db_path=self.runtime_config.paths.db_path,
                review_dir=self.runtime_config.paths.review_dir,
                web_state_dir=web_state_dir,
            )
        )
        lock_path = repository_root / ".private-av1-runtime.lock"
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.mediaforce_runtime_lock_path_for_web_state_dir",
                return_value=lock_path,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.mediaforce_runtime_lock_path",
                return_value=lock_path,
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "must remain outside the repository",
            ),
        ):
            _validated_av1_validation_derivation_artifact_root(
                config=config,
                plan=self.plan,
                artifact_root=artifact_root,
            )

    def test_direct_derivation_artifact_write_holds_runtime_lock(self) -> None:
        lock_held = False

        @contextmanager
        def runtime_lock(
                _config: object,
                *,
                owner_payload: dict[str, object],
        ) -> Iterator[None]:
            nonlocal lock_held
            self.assertEqual(owner_payload["purpose"], "av1-derivation-tooling")
            self.assertEqual(owner_payload["action"], "record-derivation-review")
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

        def stop_inside_lock(**kwargs: object) -> tuple[object, Path]:
            self.assertTrue(lock_held)
            self.assertIs(kwargs.get("config"), self.runtime_config)
            raise RuntimeError("stopped inside runtime lock")

        args = SimpleNamespace(
            action="record-derivation-review",
            config=Path("unused.toml"),
            plan=self.runtime_artifact_root / "plan.json",
        )
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_config",
                return_value=self.runtime_config,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "exclusive_mediaforce_runtime_lock",
                side_effect=runtime_lock,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_canonical_derivation_plan",
                side_effect=stop_inside_lock,
            ),
            self.assertRaisesRegex(RuntimeError, "inside runtime lock"),
        ):
            verify_av1_cold_start_preregistration._run_derivation_action(args)
        self.assertFalse(lock_held)

    def test_direct_derivation_artifact_write_rejects_busy_runtime(self) -> None:
        args = SimpleNamespace(
            action="record-derivation-review",
            config=Path("unused.toml"),
        )
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_config",
                return_value=self.runtime_config,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "exclusive_mediaforce_runtime_lock",
                side_effect=MediaforceRuntimeBusyError(
                    "Mediaforce runtime is already active"
                ),
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "runtime to be paused",
            ),
        ):
            verify_av1_cold_start_preregistration._run_derivation_action(args)

    def test_copied_preregistration_runner_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied_runner = Path(directory) / "verify_av1.py"
            copied_runner.write_text("# copied runner\n", encoding="utf-8")
            with (
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "__file__",
                    str(copied_runner),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "not the canonical repository file",
                ),
            ):
                verify_av1_cold_start_preregistration._assert_canonical_preregistration_runner()

    def test_derivation_review_checks_environment_before_claim(self) -> None:
        args = SimpleNamespace(
            action="record-derivation-review",
            config=Path("unused.toml"),
            plan=self.runtime_artifact_root / "plan.json",
            cell_plan_id=self.plan.assignments[0].cell_plan_id,
            lane="architecture",
        )
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_canonical_derivation_plan",
                return_value=(self.plan, self.runtime_artifact_root),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "assert_av1_validation_derivation_execution_environment",
                side_effect=AV1ValidationDerivationError(
                    "execution environment drifted"
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_derivation_candidate_proposal",
            ) as load_proposal,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_run_code_agent_review",
            ) as run_review,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "execution environment drifted",
            ),
        ):
            verify_av1_cold_start_preregistration._run_derivation_action_body(
                args,
                locked_config=self.runtime_config,
            )
        load_proposal.assert_not_called()
        run_review.assert_not_called()

    def test_review_retry_reuses_complete_envelope_after_parent_fsync_failure(
            self,
    ) -> None:
        proposal = self._candidate_proposal()
        write_av1_validation_derivation_candidate_proposal(
            self.runtime_artifact_root,
            plan=self.plan,
            proposal=proposal,
        )
        claim = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            repository_commit=REVIEW_REPOSITORY_COMMIT,
            repository_tree=REVIEW_REPOSITORY_TREE,
            lane="architecture",
            review_run_id="81000000-0000-0000-0000-000000000001",
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=self.plan.review_runner_binary_sha256,
            claimed_at="2026-07-28T03:00:01Z",
        )
        evidence = _review_run_evidence(
            proposal=proposal,
            claim=claim,
        )
        review = build_av1_validation_derivation_review_attestation(
            proposal=proposal,
            claim=claim,
            review_evidence_sha256=(
                f"sha256:{hashlib.sha256(evidence).hexdigest()}"
            ),
            decision="approved",
            reviewed_at="2026-07-28T03:30:00Z",
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
        review_path = (
            self.runtime_artifact_root
            / "reviews"
            / proposal.proposal_id
            / "architecture.json"
        )
        review_published = False
        real_fsync = os.fsync
        real_rename = _rename_owner_only_exclusive

        def fail_review_publish_fsync(descriptor: int) -> None:
            if review_published and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(errno.EIO, "directory fsync failed")
            real_fsync(descriptor)

        def track_review_publish(
                *,
                parent_descriptor: int,
                source_name: str,
                destination_name: str,
        ) -> None:
            nonlocal review_published
            real_rename(
                parent_descriptor=parent_descriptor,
                source_name=source_name,
                destination_name=destination_name,
            )
            if destination_name == "architecture.json":
                review_published = True

        with (
            patch(
                "mediaforce.tuning.av1_validation_derivation._rename_owner_only_exclusive",
                side_effect=track_review_publish,
            ),
            patch(
                "mediaforce.tuning.av1_validation_derivation.os.fsync",
                side_effect=fail_review_publish_fsync,
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "could not be written safely",
            ),
        ):
            write_av1_validation_derivation_review_envelope(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
                envelope=envelope,
            )
        self.assertTrue(review_path.exists())

        args = SimpleNamespace(
            action="record-derivation-review",
            config=Path("unused.toml"),
            plan=self.runtime_artifact_root / "plan.json",
            cell_plan_id=proposal.cell_plan_id,
            lane="architecture",
            json_output=True,
        )
        recovered_parent_sync = False

        def track_recovery_fsync(descriptor: int) -> None:
            nonlocal recovered_parent_sync
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                recovered_parent_sync = True
            real_fsync(descriptor)

        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_canonical_derivation_plan",
                return_value=(self.plan, self.runtime_artifact_root),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "assert_av1_validation_derivation_execution_environment",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_repository_review_identity",
                return_value=(
                    self.plan.repository_commit,
                    self.plan.repository_tree,
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_run_code_agent_review",
            ) as run_review,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_now_iso",
            ) as now_iso,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_print_partition_payload",
            ),
            patch(
                "mediaforce.tuning.av1_validation_derivation.os.fsync",
                side_effect=track_recovery_fsync,
            ),
        ):
            exit_code = (
                verify_av1_cold_start_preregistration._run_derivation_action_body(
                    args,
                    locked_config=self.runtime_config,
                )
            )
        self.assertEqual(exit_code, 0)
        self.assertTrue(recovered_parent_sync)
        run_review.assert_not_called()
        now_iso.assert_not_called()
        recovered = load_av1_validation_derivation_review_envelope(
            self.runtime_artifact_root,
            plan=self.plan,
            proposal=proposal,
            claim=claim,
        )
        self.assertEqual(recovered, envelope)
        self.assertEqual(recovered.review.reviewed_at, "2026-07-28T03:30:00Z")

        conflicting_review = build_av1_validation_derivation_review_attestation(
            proposal=proposal,
            claim=claim,
            review_evidence_sha256=review.review_evidence_sha256,
            decision="approved",
            reviewed_at="2026-07-28T03:31:00Z",
        )
        conflicting_envelope = build_av1_validation_derivation_review_envelope(
            review=conflicting_review,
            evidence=evidence,
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "conflicts with an immutable existing review",
        ):
            write_av1_validation_derivation_review_envelope(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
                envelope=conflicting_envelope,
            )

    def test_review_retry_rejects_unresolved_claim_without_launching_agent(self) -> None:
        proposal = self._candidate_proposal()
        write_av1_validation_derivation_candidate_proposal(
            self.runtime_artifact_root,
            plan=self.plan,
            proposal=proposal,
        )
        claim = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            repository_commit=REVIEW_REPOSITORY_COMMIT,
            repository_tree=REVIEW_REPOSITORY_TREE,
            lane="architecture",
            review_run_id="82000000-0000-0000-0000-000000000001",
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=self.plan.review_runner_binary_sha256,
            claimed_at="2026-07-28T03:00:01Z",
        )
        write_av1_validation_derivation_review_claim(
            self.runtime_artifact_root,
            plan=self.plan,
            proposal=proposal,
            claim=claim,
        )
        args = SimpleNamespace(
            action="record-derivation-review",
            config=Path("unused.toml"),
            plan=self.runtime_artifact_root / "plan.json",
            cell_plan_id=proposal.cell_plan_id,
            lane="architecture",
            json_output=True,
        )
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_canonical_derivation_plan",
                return_value=(self.plan, self.runtime_artifact_root),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "assert_av1_validation_derivation_execution_environment",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_run_code_agent_review",
            ) as run_review,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_now_iso",
            ) as now_iso,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "terminal and cannot be resumed",
            ),
        ):
            verify_av1_cold_start_preregistration._run_derivation_action_body(
                args,
                locked_config=self.runtime_config,
            )
        run_review.assert_not_called()
        now_iso.assert_not_called()

    def test_immutable_write_failure_cleans_unpublished_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            with (
                patch(
                    "mediaforce.tuning.av1_validation_derivation.os.write",
                    side_effect=OSError(errno.EIO, "write failed"),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "could not be written safely",
                ),
            ):
                _write_owner_only(root / "artifact.json", b"{}")
            artifact_path = root / "artifact.json"
            self.assertFalse(artifact_path.exists())
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_immutable_write_publishes_complete_file_with_exclusive_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            artifact_path = root / "artifact.json"
            payload = b'{"complete":true}'

            def inspect_then_rename(
                    *,
                    parent_descriptor: int,
                    source_name: str,
                    destination_name: str,
            ) -> None:
                self.assertFalse(artifact_path.exists())
                temporary_path = root / source_name
                self.assertEqual(temporary_path.read_bytes(), payload)
                self.assertEqual(temporary_path.stat().st_mode & 0o777, 0o400)
                _rename_owner_only_exclusive(
                    parent_descriptor=parent_descriptor,
                    source_name=source_name,
                    destination_name=destination_name,
                )

            with patch(
                "mediaforce.tuning.av1_validation_derivation._rename_owner_only_exclusive",
                side_effect=inspect_then_rename,
            ):
                _write_owner_only(artifact_path, payload)
            self.assertEqual(artifact_path.read_bytes(), payload)
            self.assertEqual(artifact_path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_immutable_write_temporary_collision_is_not_publish_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            artifact_path = root / "artifact.json"
            temporary_path = root / ".artifact.json.collision.tmp"
            temporary_path.write_bytes(b"occupied")
            temporary_path.chmod(0o400)
            with (
                patch(
                    "mediaforce.tuning.av1_validation_derivation.secrets.token_hex",
                    return_value="collision",
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "could not be written safely",
                ),
            ):
                _write_owner_only(artifact_path, b"{}")
            self.assertFalse(artifact_path.exists())
            self.assertEqual(temporary_path.read_bytes(), b"occupied")

    def test_binding_does_not_accept_post_publish_durability_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binding_directory = Path(directory) / "bindings"
            binding_directory.mkdir(mode=0o700)
            with (
                patch(
                    "mediaforce.tuning.av1_validation_derivation._fsync_owner_only_artifact"
                ),
                patch(
                    "mediaforce.tuning.av1_validation_derivation.os.fsync",
                    side_effect=OSError(errno.EIO, "directory fsync failed"),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "could not be written safely",
                ),
            ):
                _bind_owner_only_directory(
                    binding_directory,
                    kind="test",
                    binding_id="binding1_immutable",
                    binding_digest="sha256:" + "1" * 64,
                )
            self.assertTrue((binding_directory / ".binding").exists())

    def test_verdict_claim_does_not_accept_post_publish_durability_failure(self) -> None:
        attempt = self._review_pending_attempt()
        claims_directory = self.runtime_artifact_root / "verdict-claims"
        _bind_owner_only_directory(
            claims_directory,
            kind="verdict_claims",
            binding_id=self.plan.plan_id,
            binding_digest=self.plan.authorization.authorization_id,
        )
        directory_fsyncs = 0
        real_fsync = os.fsync

        def tracked_failure(descriptor: int) -> None:
            nonlocal directory_fsyncs
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                directory_fsyncs += 1
                if directory_fsyncs == 3:
                    raise OSError(errno.EIO, "directory fsync failed")
            real_fsync(descriptor)

        with (
            patch(
                "mediaforce.tuning.av1_validation_derivation._fsync_owner_only_artifact"
            ),
            patch(
                "mediaforce.tuning.av1_validation_derivation.os.fsync",
                side_effect=tracked_failure,
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "could not be written safely",
            ),
        ):
            ensure_av1_validation_derivation_verdict_claim(
                claims_directory,
                plan=self.plan,
                attempt=attempt,
                claimed_at="2026-07-28T01:06:00Z",
            )
        self.assertTrue(
            (claims_directory / f"{attempt.assignment_id}.json").exists()
        )
        synced_directory = False
        real_fsync = os.fsync

        def track_fsync(descriptor: int) -> None:
            nonlocal synced_directory
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                synced_directory = True
            real_fsync(descriptor)

        with patch(
            "mediaforce.tuning.av1_validation_derivation.os.fsync",
            side_effect=track_fsync,
        ):
            self.assertFalse(ensure_av1_validation_derivation_verdict_claim(
                claims_directory,
                plan=self.plan,
                attempt=attempt,
                claimed_at="2026-07-28T01:06:00Z",
            ))
        self.assertTrue(synced_directory)

    def test_immutable_read_rejects_matching_content_path_substitution(self) -> None:
        plan_path = self.runtime_artifact_root / "plan.json"
        original_path = self.runtime_artifact_root / "plan-original.json"
        original_read = os.read
        path_swapped = False

        def swap_path_then_read(descriptor: int, size: int) -> bytes:
            nonlocal path_swapped
            if not path_swapped:
                path_swapped = True
                plan_bytes = plan_path.read_bytes()
                plan_path.rename(original_path)
                plan_path.write_bytes(plan_bytes)
                plan_path.chmod(0o400)
            return original_read(descriptor, size)

        with (
            patch(
                "mediaforce.tuning.av1_validation_derivation.os.read",
                side_effect=swap_path_then_read,
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "changed while it was being read",
            ),
        ):
            load_av1_validation_derivation_plan(plan_path)

    def test_owner_only_publication_rejects_post_deadline_rename(self) -> None:
        self.publication_time_patcher.stop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            artifact_path = root / "artifact.json"
            before_publish = unittest.mock.Mock()

            with self.assertRaises(AV1ValidationDerivationPublicationDeadlineError):
                _write_owner_only(
                    artifact_path,
                    b"{}",
                    before_publish=before_publish,
                    published_before=AUTHORIZED_AT,
                )

            before_publish.assert_called_once_with()
            self.assertTrue(artifact_path.exists())
            with self.assertRaises(AV1ValidationDerivationPublicationDeadlineError):
                _read_owner_only_bytes(
                    artifact_path,
                    "test artifact",
                    published_before=AUTHORIZED_AT,
                )

    def test_assignment_claim_loader_marks_post_deadline_publication(self) -> None:
        claims_directory = self.runtime_artifact_root / "attempts"
        assignment = self.plan.assignments[0]
        write_av1_validation_derivation_assignment_claim(
            claims_directory,
            assignment_id=assignment.assignment_id,
            plan_id=self.plan.plan_id,
            authorization_id=self.plan.authorization.authorization_id,
            claimed_at="2026-07-28T01:00:00Z",
        )

        with patch(
            "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
            return_value=10**30,
        ):
            claims = load_av1_validation_derivation_assignment_claims(
                claims_directory,
                plan=self.plan,
            )
        self.assertEqual(len(claims), 1)
        self.assertTrue(claims[0]["published_after_deadline"])

    def test_exclusive_rename_advances_kernel_change_time(self) -> None:
        self.publication_time_patcher.stop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            source_path = root / ".artifact.tmp"
            destination_path = root / "artifact.json"
            source_path.write_bytes(b"{}")
            source_path.chmod(0o400)
            before = source_path.stat().st_ctime_ns
            time.sleep(0.01)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                _rename_owner_only_exclusive(
                    parent_descriptor=descriptor,
                    source_name=source_path.name,
                    destination_name=destination_path.name,
                )
            finally:
                os.close(descriptor)

            self.assertGreater(destination_path.stat().st_ctime_ns, before)

    def test_immutable_read_preserves_primary_error_when_close_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            artifact_path = root / "artifact.json"
            _write_owner_only(artifact_path, b"{}")
            parent_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            real_close = os.close

            def close_then_fail(descriptor: int) -> None:
                real_close(descriptor)
                raise OSError(errno.EIO, "close failed")

            with (
                patch(
                    "mediaforce.tuning.av1_validation_derivation.open_stable_directory",
                    return_value=(root, parent_descriptor),
                ),
                patch(
                    "mediaforce.tuning.av1_validation_derivation.os.read",
                    side_effect=OSError(errno.EIO, "read failed"),
                ),
                patch(
                    "mediaforce.tuning.av1_validation_derivation.os.close",
                    side_effect=close_then_fail,
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "is unavailable",
                ),
            ):
                _read_owner_only_bytes(artifact_path, "test artifact")

    def test_immutable_read_reports_close_failure_inside_outer_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            artifact_path = root / "artifact.json"
            _write_owner_only(artifact_path, b"{}")
            parent_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            real_close = os.close

            def close_then_fail(descriptor: int) -> None:
                real_close(descriptor)
                raise OSError(errno.EIO, "close failed")

            try:
                raise RuntimeError("outer failure")
            except RuntimeError:
                with (
                    patch(
                        "mediaforce.tuning.av1_validation_derivation.open_stable_directory",
                        return_value=(root, parent_descriptor),
                    ),
                    patch(
                        "mediaforce.tuning.av1_validation_derivation.os.close",
                        side_effect=close_then_fail,
                    ),
                    self.assertRaisesRegex(
                        AV1ValidationDerivationError,
                        "cleanup failed",
                    ),
                ):
                    _read_owner_only_bytes(artifact_path, "test artifact")

    def test_assignment_and_review_claim_directories_are_fsynced_through_parents(self) -> None:
        original_fsync = os.fsync

        def fsynced_directory_identities() -> tuple[set[tuple[int, int]], object]:
            identities: set[tuple[int, int]] = set()

            def track_fsync(descriptor: int) -> None:
                info = os.fstat(descriptor)
                if stat.S_ISDIR(info.st_mode):
                    identities.add((int(info.st_dev), int(info.st_ino)))
                original_fsync(descriptor)

            return identities, track_fsync

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts_directory = root / "nested" / "claims" / "attempts"
            fsynced_identities, track_fsync = fsynced_directory_identities()
            with patch(
                "mediaforce.core.file_integrity.os.fsync",
                side_effect=track_fsync,
            ):
                write_av1_validation_derivation_assignment_claim(
                    attempts_directory,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    plan_id=self.plan.plan_id,
                    authorization_id=self.plan.authorization.authorization_id,
                    claimed_at="2026-07-28T01:00:00Z",
                )
            expected_identities = {
                (int(path.stat().st_dev), int(path.stat().st_ino))
                for path in (
                    root,
                    root / "nested",
                    root / "nested" / "claims",
                    attempts_directory,
                )
            }
            self.assertTrue(expected_identities.issubset(fsynced_identities))

        proposal = self._candidate_proposal()
        claim = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            repository_commit=REVIEW_REPOSITORY_COMMIT,
            repository_tree=REVIEW_REPOSITORY_TREE,
            lane="architecture",
            review_run_id="70000000-0000-0000-0000-000000000001",
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=(
                self.plan.review_runner_binary_sha256
            ),
            claimed_at="2026-07-28T03:00:01Z",
        )
        fsynced_identities, track_fsync = fsynced_directory_identities()
        with patch(
            "mediaforce.core.file_integrity.os.fsync",
            side_effect=track_fsync,
        ):
            write_av1_validation_derivation_review_claim(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
            )
        resolved_artifact_root = self.runtime_artifact_root.resolve()
        expected_identities = {
            (int(path.stat().st_dev), int(path.stat().st_ino))
            for path in (
                resolved_artifact_root,
                resolved_artifact_root / "review-claims",
                resolved_artifact_root / "review-claims" / proposal.proposal_id,
            )
        }
        self.assertTrue(expected_identities.issubset(fsynced_identities))

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
            repository_commit=REVIEW_REPOSITORY_COMMIT,
            repository_tree=REVIEW_REPOSITORY_TREE,
            lane="architecture",
            review_run_id=agent_id,
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=(
                self.plan.review_runner_binary_sha256
            ),
            claimed_at="2026-07-28T03:00:00Z",
        )
        marker = {
            "decision": "approved",
            "lane": "architecture",
            "proposal_id": proposal.proposal_id,
            "proposal_payload_sha256": proposal.payload_sha256,
            "repository_commit": REVIEW_REPOSITORY_COMMIT,
            "repository_tree": REVIEW_REPOSITORY_TREE,
            "review_claim_id": expected_claim.claim_id,
            "review_claim_payload_sha256": expected_claim.payload_sha256,
            "review_run_id": agent_id,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code_binary = root / "code"
            isolated_repository = root / "isolated-repository"
            code_binary.write_bytes(REVIEW_RUNNER_BYTES)
            code_binary.chmod(0o700)
            final_message = (
                "Résumé\u2028review\u2029complete.\n"
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
                }, ensure_ascii=False),
                json.dumps({"prompt": prompt}, ensure_ascii=False),
                json.dumps({
                    "msg": {
                        "type": "agent_message",
                        "message": final_message,
                    }
                }, ensure_ascii=False),
                json.dumps({
                    "msg": {
                        "type": "task_lifecycle",
                        "phase": "quiescent",
                        "last_agent_message": final_message,
                    }
                }, ensure_ascii=False),
            ))
            completed = SimpleNamespace(
                returncode=0,
                stdout=stdout,
                stderr="",
            )
            with (
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_repository_review_identity",
                    return_value=(
                        REVIEW_REPOSITORY_COMMIT,
                        REVIEW_REPOSITORY_TREE,
                    ),
                ) as repository_identity,
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_authorized_review_runner_identity",
                    return_value=(
                        code_binary,
                        self.plan.review_runner_canonical_path_sha256,
                        self.plan.review_runner_binary_sha256,
                        REVIEW_RUNNER_BYTES,
                    ),
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_isolated_review_repository",
                    return_value=_context_value(isolated_repository),
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "run_command",
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
            self.assertFalse(launched_runner.parent.exists())
            review_command = run_review.call_args.args[0]
            self.assertIn('shell_environment_policy.inherit="none"', review_command)
            self.assertEqual(
                run_review.call_args.kwargs["cwd"],
                isolated_repository,
            )
            self.assertNotEqual(
                run_review.call_args.kwargs["cwd"],
                verify_av1_cold_start_preregistration.REPOSITORY_ROOT,
            )
            review_environment = run_review.call_args.kwargs["env"]
            self.assertEqual(
                review_environment["PATH"],
                verify_av1_cold_start_preregistration._AGENT_REVIEW_SAFE_PATH,
            )
            evidence_payload = json.loads(evidence)
            self.assertEqual(evidence, canonical_json_bytes(evidence_payload))
            self.assertEqual(evidence_payload["review_run_id"], agent_id)
            self.assertEqual(evidence_payload["returncode"], 0)
            self.assertEqual(
                evidence_payload["repository_commit"],
                REVIEW_REPOSITORY_COMMIT,
            )
            self.assertEqual(
                evidence_payload["repository_tree"],
                REVIEW_REPOSITORY_TREE,
            )
            self.assertEqual(repository_identity.call_count, 2)
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

    def test_review_recorder_rejects_noncanonical_transcript(self) -> None:
        agent_id = "12345678-1234-1234-1234-123456789abd"
        proposal = self._candidate_proposal()
        expected_claim = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            repository_commit=REVIEW_REPOSITORY_COMMIT,
            repository_tree=REVIEW_REPOSITORY_TREE,
            lane="architecture",
            review_run_id=agent_id,
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=(
                self.plan.review_runner_binary_sha256
            ),
            claimed_at="2026-07-28T03:00:00Z",
        )
        prompt = verify_av1_cold_start_preregistration._agent_review_prompt(
            proposal=proposal,
            claim=expected_claim,
        )
        with tempfile.TemporaryDirectory() as directory:
            code_binary = Path(directory) / "code"
            isolated_repository = Path(directory) / "isolated-repository"
            code_binary.write_bytes(REVIEW_RUNNER_BYTES)
            code_binary.chmod(0o700)
            completed = SimpleNamespace(
                returncode=0,
                stdout="\n".join((
                    json.dumps({
                        "provider": "test",
                        "model": "test-model",
                        "workdir": directory,
                        "approval": "never",
                        "sandbox": "read-only",
                    }),
                    json.dumps({"prompt": prompt}),
                    "not-json",
                )),
                stderr="",
            )
            with (
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_repository_review_identity",
                    return_value=(
                        REVIEW_REPOSITORY_COMMIT,
                        REVIEW_REPOSITORY_TREE,
                    ),
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_authorized_review_runner_identity",
                    return_value=(
                        code_binary,
                        self.plan.review_runner_canonical_path_sha256,
                        self.plan.review_runner_binary_sha256,
                        REVIEW_RUNNER_BYTES,
                    ),
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_isolated_review_repository",
                    return_value=_context_value(isolated_repository),
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "run_command",
                    return_value=completed,
                ),
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
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "not canonical JSONL",
                ),
            ):
                verify_av1_cold_start_preregistration._run_code_agent_review(
                    artifact_root=self.runtime_artifact_root,
                    plan=self.plan,
                    proposal=proposal,
                    lane="architecture",
                )
            self.assertEqual(
                load_av1_validation_derivation_review_claims(
                    self.runtime_artifact_root,
                    plan=self.plan,
                    proposal=proposal,
                ),
                (expected_claim,),
            )

    def test_review_transcript_requires_ordered_valid_config_and_prompt(self) -> None:
        config = {
            "provider": "test",
            "model": "test-model",
            "workdir": "/private/test",
            "approval": "never",
            "sandbox": "read-only",
        }
        prompt = {"prompt": "review prompt"}
        final_message = "Review complete."
        message = {
            "msg": {
                "type": "agent_message",
                "message": final_message,
            }
        }
        completion = {
            "msg": {
                "type": "task_lifecycle",
                "phase": "quiescent",
                "last_agent_message": final_message,
            }
        }
        cases = (
            (
                "malformed config",
                ({**config, "provider": None}, prompt, message, completion),
                "configuration is invalid",
            ),
            (
                "config with message fields",
                (
                    {
                        **config,
                        "msg": {
                            "type": "agent_message",
                            "message": final_message,
                        },
                    },
                    prompt,
                    message,
                    completion,
                ),
                "configuration is invalid",
            ),
            (
                "duplicate prompt",
                (config, prompt, prompt, message, completion),
                "prompt is duplicated or out of order",
            ),
            (
                "smuggled duplicate prompt",
                (
                    config,
                    prompt,
                    {
                        "prompt": "drifted prompt",
                        "msg": {
                            "type": "agent_message",
                            "message": final_message,
                        },
                    },
                    completion,
                ),
                "prompt is duplicated or out of order",
            ),
            (
                "smuggled config field",
                (
                    config,
                    prompt,
                    {
                        "provider": "drifted-provider",
                        "msg": {
                            "type": "agent_message",
                            "message": final_message,
                        },
                    },
                    completion,
                ),
                "configuration is duplicated or out of order",
            ),
            (
                "prompt after completion",
                (config, prompt, message, completion, prompt),
                "events after completion",
            ),
            (
                "malformed early completion",
                (
                    config,
                    prompt,
                    {
                        "msg": {
                            "type": "task_lifecycle",
                            "phase": "quiescent",
                            "last_agent_message": None,
                        }
                    },
                    message,
                    completion,
                ),
                "completion is invalid",
            ),
        )
        for label, events, expected_error in cases:
            with self.subTest(label=label), self.assertRaisesRegex(
                AV1ValidationDerivationError,
                expected_error,
            ):
                _completed_code_review_message("\n".join(
                    canonical_json_bytes(event).decode("utf-8")
                    for event in events
                ))

        duplicate_key_lines = (
            (
                "duplicate prompt key",
                '{"prompt":"first","prompt":"review prompt"}',
            ),
            (
                "duplicate config key",
                (
                    '{"provider":"first","provider":"test",'
                    '"model":"test-model","workdir":"/private/test",'
                    '"approval":"never","sandbox":"read-only"}'
                ),
            ),
        )
        for label, duplicate_line in duplicate_key_lines:
            lines = [
                canonical_json_bytes(config).decode("utf-8"),
                canonical_json_bytes(prompt).decode("utf-8"),
                canonical_json_bytes(message).decode("utf-8"),
                canonical_json_bytes(completion).decode("utf-8"),
            ]
            lines[0 if "config" in label else 1] = duplicate_line
            with self.subTest(label=label), self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "duplicate JSON keys",
            ):
                _completed_code_review_message("\n".join(lines))

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "duplicate JSON keys",
        ):
            _code_review_marker(
                'MEDIAFORCE_AV1_REVIEW_V2 '
                '{"decision":"approved","decision":"rejected"}'
            )

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
                    self.plan.review_runner_binary_sha256,
                    REVIEW_RUNNER_BYTES,
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_command",
            ) as run_review,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "drifted from the plan",
            ),
        ):
            verify_av1_cold_start_preregistration._run_code_agent_review(
                artifact_root=self.runtime_artifact_root,
                plan=self.plan,
                proposal=self._candidate_proposal(),
                lane="architecture",
            )
        run_review.assert_not_called()

    def test_repository_review_identity_requires_clean_tracked_state(self) -> None:
        controller = ManagedProcessController()
        identity_result = SimpleNamespace(
            returncode=0,
            stdout=(
                f"{REVIEW_REPOSITORY_COMMIT}\n"
                f"{REVIEW_REPOSITORY_TREE}\n"
            ),
        )
        clean_result = SimpleNamespace(returncode=0, stdout="")
        with patch.object(
            verify_av1_cold_start_preregistration,
            "run_command",
            side_effect=(identity_result, clean_result, identity_result),
        ) as run_git:
            identity = (
                verify_av1_cold_start_preregistration._repository_review_identity(
                    process_controller=controller,
                )
            )
        self.assertEqual(
            identity,
            (REVIEW_REPOSITORY_COMMIT, REVIEW_REPOSITORY_TREE),
        )
        self.assertEqual(run_git.call_count, 3)
        self.assertEqual(run_git.call_args_list[0].args[0][:3], [
            "/usr/bin/git",
            "rev-parse",
            "HEAD",
        ])
        self.assertEqual(
            run_git.call_args_list[1].args[0][3],
            REVIEW_REPOSITORY_COMMIT,
        )

        dirty_result = SimpleNamespace(returncode=1, stdout="")
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_command",
                side_effect=(identity_result, dirty_result),
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "uncommitted tracked changes",
            ),
        ):
            verify_av1_cold_start_preregistration._repository_review_identity(
                process_controller=controller,
            )

    def test_repository_review_identity_rejects_mid_check_ref_drift(self) -> None:
        controller = ManagedProcessController()
        identity_result = SimpleNamespace(
            returncode=0,
            stdout=(
                f"{REVIEW_REPOSITORY_COMMIT}\n"
                f"{REVIEW_REPOSITORY_TREE}\n"
            ),
        )
        drifted_result = SimpleNamespace(
            returncode=0,
            stdout=(f"{'b' * 40}\n{'c' * 40}\n"),
        )
        clean_result = SimpleNamespace(returncode=0, stdout="")
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_command",
                side_effect=(identity_result, clean_result, drifted_result),
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "changed during verification",
            ),
        ):
            verify_av1_cold_start_preregistration._repository_review_identity(
                process_controller=controller,
            )

    def test_assignment_action_passes_live_repository_identity_resolver(self) -> None:
        args = SimpleNamespace(
            action="run-derivation-assignment",
            config=Path("unused.toml"),
            manifest=Path("manifest.json"),
            partition=Path("partition.json"),
            plan=Path("plan.json"),
            key=Path("partition.key"),
            assignment_id=self.plan.assignments[0].assignment_id,
            json_output=True,
        )
        attempt = SimpleNamespace(
            attempt_id="attempt-test",
            payload_sha256="sha256:" + "d" * 64,
            status="review_pending",
        )
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "assert_private_artifact_path",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_manifest_v2",
                return_value=self.manifest,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_private_partition",
                return_value=self.partition,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_recovery_capable_derivation_plan",
                return_value=(self.plan, self.runtime_artifact_root),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_partition_key",
                return_value=self.token_key,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_repository_review_identity",
                return_value=self._matching_repository_identity(),
            ) as repository_identity,
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_av1_validation_derivation_assignment",
                return_value=attempt,
            ) as run_assignment,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_print_partition_payload",
            ),
        ):
            exit_code = verify_av1_cold_start_preregistration._run_derivation_action_body(
                args,
                locked_config=None,
            )
            process_controller = run_assignment.call_args.kwargs["process_controller"]
            resolver = run_assignment.call_args.kwargs["repository_identity_resolver"]
            first_identity = resolver()
            second_identity = resolver()

        self.assertEqual(exit_code, 0)
        self.assertIsInstance(process_controller, ManagedProcessController)
        self.assertEqual(first_identity, self._matching_repository_identity())
        self.assertEqual(second_identity, self._matching_repository_identity())
        self.assertEqual(repository_identity.call_count, 2)
        repository_identity.assert_has_calls([
            call(process_controller=process_controller),
            call(process_controller=process_controller),
        ])

    def test_isolated_review_repository_excludes_live_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_repository = root / "source"
            source_repository.mkdir()
            _run_test_git(source_repository, "init", "--quiet")
            (source_repository / "tracked.txt").write_text(
                "committed\n",
                encoding="utf-8",
            )
            _run_test_git(source_repository, "add", "tracked.txt")
            _run_test_git(source_repository, "commit", "--quiet", "-m", "initial")
            repository_commit = _run_test_git(
                source_repository,
                "rev-parse",
                "HEAD",
            )
            repository_tree = _run_test_git(
                source_repository,
                "rev-parse",
                "HEAD^{tree}",
            )
            live_worktree = root / "live-worktree"
            _run_test_git(
                source_repository,
                "worktree",
                "add",
                "--quiet",
                "--detach",
                str(live_worktree),
                repository_commit,
            )
            (live_worktree / "live-only.txt").write_text(
                "must not be reviewed\n",
                encoding="utf-8",
            )
            isolated_root: Path | None = None

            with patch.object(
                verify_av1_cold_start_preregistration,
                "REPOSITORY_ROOT",
                live_worktree,
            ):
                with verify_av1_cold_start_preregistration._isolated_review_repository(
                    repository_commit=repository_commit,
                    repository_tree=repository_tree,
                    process_controller=ManagedProcessController(),
                ) as isolated_repository:
                    isolated_root = isolated_repository.parent
                    self.assertEqual(
                        stat.S_IMODE(isolated_root.stat().st_mode),
                        0o700,
                    )
                    self.assertEqual(
                        stat.S_IMODE(isolated_repository.stat().st_mode),
                        0o700,
                    )
                    self.assertEqual(
                        (isolated_repository / "tracked.txt").read_text(
                            encoding="utf-8"
                        ),
                        "committed\n",
                    )
                    self.assertFalse(
                        (isolated_repository / "live-only.txt").exists()
                    )
                    self.assertEqual(
                        _run_test_git(isolated_repository, "remote"),
                        "",
                    )

            assert isolated_root is not None
            self.assertFalse(isolated_root.exists())

    def test_isolated_review_repository_identity_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_repository = root / "source"
            source_repository.mkdir()
            _run_test_git(source_repository, "init", "--quiet")
            tracked_path = source_repository / "tracked.txt"
            tracked_path.write_text("first\n", encoding="utf-8")
            _run_test_git(source_repository, "add", "tracked.txt")
            _run_test_git(source_repository, "commit", "--quiet", "-m", "first")
            first_commit = _run_test_git(source_repository, "rev-parse", "HEAD")
            first_tree = _run_test_git(
                source_repository,
                "rev-parse",
                "HEAD^{tree}",
            )
            tracked_path.write_text("second\n", encoding="utf-8")
            _run_test_git(source_repository, "commit", "--quiet", "-am", "second")
            second_commit = _run_test_git(source_repository, "rev-parse", "HEAD")
            isolated_root: Path | None = None

            with (
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "REPOSITORY_ROOT",
                    source_repository,
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "changed during review",
                ),
            ):
                with verify_av1_cold_start_preregistration._isolated_review_repository(
                    repository_commit=first_commit,
                    repository_tree=first_tree,
                    process_controller=ManagedProcessController(),
                ) as isolated_repository:
                    isolated_root = isolated_repository.parent
                    _run_test_git(
                        isolated_repository,
                        "checkout",
                        "--quiet",
                        "--detach",
                        "--force",
                        second_commit,
                    )

            assert isolated_root is not None
            self.assertFalse(isolated_root.exists())

    def test_review_runner_is_reverified_after_launch(self) -> None:
        proposal = self._candidate_proposal()
        before_identity = (
            Path("/private/authorized-code"),
            self.plan.review_runner_canonical_path_sha256,
            self.plan.review_runner_binary_sha256,
            REVIEW_RUNNER_BYTES,
        )
        after_identity = (
            Path("/private/substitute-code"),
            f"sha256:{'c' * 64}",
            self.plan.review_runner_binary_sha256,
            REVIEW_RUNNER_BYTES,
        )
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_repository_review_identity",
                return_value=(
                    REVIEW_REPOSITORY_COMMIT,
                    REVIEW_REPOSITORY_TREE,
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_review_runner_identity",
                side_effect=(before_identity, after_identity),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_isolated_review_repository",
                return_value=_context_value(Path("/private/isolated-review")),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_command",
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
                "drifted from the plan",
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
        self.assertEqual(
            (runner_directory / "code").read_bytes(),
            REVIEW_RUNNER_BYTES,
        )
        shutil.rmtree(runner_directory)

    @unittest.skipUnless(hasattr(__import__("select"), "kqueue"), "requires kqueue")
    def test_private_review_runner_detects_parent_swap_and_restore(self) -> None:
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
                moved_directory = runner_directory.with_name(
                    f"{runner_directory.name}-moved"
                )
                runner_directory.rename(moved_directory)
                try:
                    runner_directory.mkdir(mode=0o700)
                    replacement = runner_directory / runner.name
                    replacement.write_bytes(REVIEW_RUNNER_BYTES)
                    replacement.chmod(0o500)
                    self.assertEqual(runner.read_bytes(), REVIEW_RUNNER_BYTES)
                finally:
                    if runner_directory.exists():
                        replacement = runner_directory / runner.name
                        if replacement.exists():
                            replacement.chmod(0o600)
                            replacement.unlink()
                        runner_directory.rmdir()
                    moved_directory.rename(runner_directory)
        assert runner_directory is not None
        self.assertEqual(
            (runner_directory / "code").read_bytes(),
            REVIEW_RUNNER_BYTES,
        )
        shutil.rmtree(runner_directory)

    @unittest.skipUnless(hasattr(__import__("select"), "kqueue"), "requires kqueue")
    def test_private_review_runner_detects_write_and_restore(self) -> None:
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
                runner.chmod(0o700)
                runner.write_bytes(b"substitute-code-binary")
                runner.write_bytes(REVIEW_RUNNER_BYTES)
                runner.chmod(0o500)
        assert runner_directory is not None
        self.assertTrue(runner_directory.exists())
        shutil.rmtree(runner_directory)

    def test_private_review_runner_cleans_without_recursive_delete(self) -> None:
        expected_sha256 = (
            f"sha256:{hashlib.sha256(REVIEW_RUNNER_BYTES).hexdigest()}"
        )
        runner_directory: Path | None = None
        with (
            patch.object(
                verify_av1_cold_start_preregistration.shutil,
                "rmtree",
                side_effect=AssertionError("review runner must not rmtree"),
            ),
            verify_av1_cold_start_preregistration._private_review_runner(
                REVIEW_RUNNER_BYTES,
                expected_sha256=expected_sha256,
            ) as runner,
        ):
            runner_directory = runner.parent
            self.assertEqual(runner.read_bytes(), REVIEW_RUNNER_BYTES)
        assert runner_directory is not None
        self.assertFalse(runner_directory.exists())

    @unittest.skipUnless(hasattr(__import__("select"), "kqueue"), "requires kqueue")
    def test_private_review_runner_preserves_parent_replacement(self) -> None:
        expected_sha256 = (
            f"sha256:{hashlib.sha256(REVIEW_RUNNER_BYTES).hexdigest()}"
        )
        runner_directory: Path | None = None
        moved_directory: Path | None = None
        replacement_bytes = b"outside-runner-replacement"
        try:
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "changed during review",
            ):
                with verify_av1_cold_start_preregistration._private_review_runner(
                    REVIEW_RUNNER_BYTES,
                    expected_sha256=expected_sha256,
                ) as runner:
                    runner_directory = runner.parent
                    moved_directory = runner_directory.with_name(
                        f"{runner_directory.name}-moved"
                    )
                    runner_directory.rename(moved_directory)
                    runner_directory.mkdir(mode=0o700)
                    replacement = runner_directory / runner.name
                    replacement.write_bytes(replacement_bytes)
                    replacement.chmod(0o500)
            assert runner_directory is not None
            assert moved_directory is not None
            self.assertEqual(
                (runner_directory / "code").read_bytes(),
                replacement_bytes,
            )
            self.assertEqual(
                (moved_directory / "code").read_bytes(),
                REVIEW_RUNNER_BYTES,
            )
        finally:
            if runner_directory is not None:
                shutil.rmtree(runner_directory, ignore_errors=True)
            if moved_directory is not None:
                shutil.rmtree(moved_directory, ignore_errors=True)

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

    def test_attempt_requires_pinned_source_evidence(self) -> None:
        assignment = self.plan.assignments[0]
        source_identity = _source_identity(self.partition, assignment)
        mutations = (
            ("missing digest", "source_snapshot_sha256", None),
            (
                "digest drift",
                "source_snapshot_sha256",
                f"sha256:{'b' * 64}",
            ),
            (
                "identity drift",
                "source_snapshot_content_version_fingerprint",
                "unreserved-source",
            ),
            ("size drift", "source_snapshot_size_bytes", 1),
        )
        for label, field, value in mutations:
            calibration = _calibration_payload(
                assignment=assignment,
                source_identity=source_identity,
                crf=28.0,
                compatibility=_compatibility(assignment),
            )
            sample_item = calibration["sample_item"]
            assert isinstance(sample_item, dict)
            if value is None:
                sample_item.pop(field)
            else:
                sample_item[field] = value
            with self.subTest(label=label), self.assertRaises(
                AV1ValidationDerivationError
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

    def test_assignment_execution_rejects_snapshot_digest_not_matching_plan_commitment(
            self,
    ) -> None:
        assignment = self.plan.assignments[0]
        calibration = _calibration_payload(
            assignment=assignment,
            source_identity=_source_identity(self.partition, assignment),
            crf=28.0,
            compatibility=_compatibility(assignment),
        )
        sample_item = calibration["sample_item"]
        assert isinstance(sample_item, dict)
        sample_item["source_snapshot_sha256"] = f"sha256:{'f' * 64}"
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
                repository_commit=REVIEW_REPOSITORY_COMMIT,
                repository_tree=REVIEW_REPOSITORY_TREE,
                lane="architecture",
                review_run_id=f"20000000-0000-0000-0000-{index:012x}",
                review_runner_canonical_path_sha256=(
                    self.plan.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.plan.review_runner_binary_sha256
                ),
                claimed_at=f"2026-07-28T03:00:0{index}Z",
            )
            for index in (1, 2)
        ]
        barrier = threading.Barrier(2)

        def write_claim(claim: AV1ValidationDerivationReviewClaim) -> bool:
            barrier.wait()
            with self.runtime_lease.bind():
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
                plan=self.plan,
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
                repository_commit=self.plan.repository_commit,
                repository_tree=self.plan.repository_tree,
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
                repository_commit=REVIEW_REPOSITORY_COMMIT,
                repository_tree=REVIEW_REPOSITORY_TREE,
                lane=lane,
                review_run_id=f"30000000-0000-0000-0000-{index:012x}",
                review_runner_canonical_path_sha256=(
                    self.plan.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.plan.review_runner_binary_sha256
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
            repository_commit=REVIEW_REPOSITORY_COMMIT,
            repository_tree=REVIEW_REPOSITORY_TREE,
            lane="privacy_security",
            review_run_id="40000000-0000-0000-0000-000000000001",
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=(
                self.plan.review_runner_binary_sha256
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
                plan=self.plan,
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
                repository_commit=self.plan.repository_commit,
                repository_tree=self.plan.repository_tree,
            )

    def test_review_evidence_digest_must_match_canonical_evidence(self) -> None:
        proposal = self._candidate_proposal()
        claim = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            repository_commit=REVIEW_REPOSITORY_COMMIT,
            repository_tree=REVIEW_REPOSITORY_TREE,
            lane="architecture",
            review_run_id="50000000-0000-0000-0000-000000000001",
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=(
                self.plan.review_runner_binary_sha256
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

    def test_review_evidence_rejects_prompt_drift_with_matching_digest(self) -> None:
        proposal = self._candidate_proposal()
        claim = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            repository_commit=REVIEW_REPOSITORY_COMMIT,
            repository_tree=REVIEW_REPOSITORY_TREE,
            lane="architecture",
            review_run_id="50000000-0000-0000-0000-000000000002",
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=(
                self.plan.review_runner_binary_sha256
            ),
            claimed_at="2026-07-28T03:00:01Z",
        )
        evidence_payload = json.loads(
            _review_run_evidence(proposal=proposal, claim=claim)
        )
        events = [
            json.loads(line)
            for line in str(evidence_payload["stdout"]).split("\n")
        ]
        drifted_prompt = f"proposal_id={proposal.proposal_id}"
        events[1] = {"prompt": drifted_prompt}
        evidence_payload["stdout"] = "\n".join(
            canonical_json_bytes(event).decode("utf-8")
            for event in events
        )
        evidence_payload["prompt_sha256"] = (
            f"sha256:{hashlib.sha256(drifted_prompt.encode('utf-8')).hexdigest()}"
        )
        evidence = canonical_json_bytes(evidence_payload)
        review = build_av1_validation_derivation_review_attestation(
            proposal=proposal,
            claim=claim,
            review_evidence_sha256=(
                f"sha256:{hashlib.sha256(evidence).hexdigest()}"
            ),
            decision="approved",
            reviewed_at="2026-07-28T03:01:00Z",
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "prompt does not match its frozen inputs",
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
                recorded_at=VALID_UNTIL,
            )
            self.assertEqual(retry, first)
            self.assertEqual(retry["recorded_at"], "2026-07-28T01:06:00Z")

    def test_verdict_intent_rejects_nonpositive_moment_indexes(self) -> None:
        attempt = self._review_pending_attempt()
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "moment indexes are invalid",
            ),
        ):
            resolve_av1_validation_derivation_verdict_intent(
                Path(directory) / "verdict-intents",
                plan=self.plan,
                attempt=attempt,
                verdict="approved",
                concern_tags=[],
                evidence_ids=[],
                moment_indexes=[0],
                recorded_at="2026-07-28T01:06:00Z",
            )

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

    def test_attempt_requires_cira3_review_artifact_binding(self) -> None:
        assignment = self.plan.assignments[0]
        calibration = _calibration_payload(
            assignment=assignment,
            source_identity=_source_identity(self.partition, assignment),
            crf=28.0,
            compatibility=_compatibility(assignment),
        )
        calibration["review_artifact_fingerprint"] = "cira2_legacy"
        calibration["current_review_artifact_fingerprint"] = "cira2_legacy"
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

    def test_authoritative_writes_reject_renamed_away_publication_directories(
            self,
    ) -> None:
        for kind, filename in (
            ("terminal_records", "terminal.json"),
            ("candidate_locks", "candidate.json"),
            ("reviews", "review.json"),
            ("candidate_proposals", "proposal.json"),
        ):
            with self.subTest(kind=kind):
                parent = self.runtime_artifact_root / f"swap-{kind}"
                retired_parent = parent.with_name(f"{parent.name}-retired")
                replacement_parent = parent.with_name(
                    f"{parent.name}-replacement"
                )
                _bind_owner_only_directory(
                    parent,
                    kind=kind,
                    binding_id=self.plan.plan_id,
                    binding_digest=self.plan.authorization.authorization_id,
                )
                replacement_parent.mkdir(mode=0o700)

                def swap_parent() -> None:
                    parent.rename(retired_parent)
                    replacement_parent.rename(parent)

                try:
                    with self.assertRaisesRegex(
                        AV1ValidationDerivationError,
                        "directory binding drifted",
                    ):
                        _write_owner_only(
                            parent / filename,
                            b"{}",
                            before_publish=swap_parent,
                        )
                    self.assertFalse((parent / filename).exists())
                    self.assertFalse((retired_parent / filename).exists())
                finally:
                    if parent.exists():
                        parent.rename(replacement_parent)
                    if retired_parent.exists():
                        retired_parent.rename(parent)

    def test_publication_directory_swap_rolls_back_database_transaction(self) -> None:
        reserve_mediaforce_database_identity(
            self.runtime_config,
            create_if_missing=True,
        )
        with open_db(self.runtime_config.paths.db_path) as connection:
            connection.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS publication_guard_probe "
                "(value INTEGER NOT NULL)"
            )
            connection.exec_driver_sql("DELETE FROM publication_guard_probe")

        directory = self.runtime_artifact_root / "transaction-terminal-records"
        retired_directory = directory.with_name(f"{directory.name}-retired")
        replacement_directory = directory.with_name(
            f"{directory.name}-replacement"
        )
        replacement_directory.mkdir(mode=0o700)
        specifications = ((
            directory,
            "terminal_records",
            self.plan.plan_id,
            self.plan.authorization.authorization_id,
        ),)
        try:
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "directory binding drifted",
            ):
                with (
                    retain_av1_validation_derivation_publication_directories(
                        specifications
                    ) as publication_guard,
                    open_db(
                        self.runtime_config.paths.db_path,
                        before_commit=publication_guard,
                    ) as connection,
                ):
                    connection.exec_driver_sql(
                        "INSERT INTO publication_guard_probe (value) VALUES (1)"
                    )
                    _write_owner_only(directory / "terminal.json", b"{}")
                    directory.rename(retired_directory)
                    replacement_directory.rename(directory)
            self.assertFalse((directory / "terminal.json").exists())
        finally:
            if directory.exists():
                directory.rename(replacement_directory)
            if retired_directory.exists():
                retired_directory.rename(directory)

        with sqlite3.connect(self.runtime_config.paths.db_path) as connection:
            row_count = connection.execute(
                "SELECT COUNT(*) FROM publication_guard_probe"
            ).fetchone()
        self.assertEqual(row_count, (0,))

    def test_artifact_directories_are_bound_to_one_plan(self) -> None:
        second_authorization = build_av1_validation_v2_derivation_authorization(
            manifest=self.manifest,
            selection_lock_sha256=self.partition.selection_lock_sha256,
            derivation_partition_sha256=self.partition.derivation_partition_sha256,
            authorized_at="2026-07-28T00:01:00Z",
            valid_until=VALID_UNTIL,
        )
        second_plan = build_av1_validation_derivation_plan(
            manifest=self.manifest,
            partition=self.partition,
            authorization=second_authorization,
            runtime_context_sha256=self.plan.runtime_context_sha256,
            execution_environment_sha256=self.plan.execution_environment_sha256,
            statistics_contract_sha256=self.plan.statistics_contract_sha256,
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=self.plan.review_runner_binary_sha256,
            repository_commit=self.plan.repository_commit,
            repository_tree=self.plan.repository_tree,
            source_commitments=self.plan.source_commitments,
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "another artifact set|immutable existing plan",
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
                artifact_root=root,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                completed_at="2026-07-28T01:01:00Z",
            ))
            attempt = load_av1_validation_derivation_attempts(attempts_dir)[0]
            terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
            self.assertEqual(attempt.status, "stopped")
            self.assertEqual(attempt.reason_code, "interrupted_claim")
            self.assertEqual(terminal.attempt_id, attempt.attempt_id)

    def test_late_interrupted_claim_terminalizes_as_authorization_expired(self) -> None:
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
            with patch(
                "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
                return_value=10**30,
            ):
                self.assertTrue(_recover_interrupted_derivation_state(
                    plan=self.plan,
                    partition=self.partition,
                    artifact_root=root,
                    attempts_directory=attempts_dir,
                    terminal_records_directory=records_dir,
                    completed_at="2026-07-28T01:01:00Z",
                ))
            attempt = load_av1_validation_derivation_attempts(attempts_dir)[0]
            terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
            self.assertEqual(attempt.status, "failed")
            self.assertEqual(attempt.reason_code, "authorization_expired")
            self.assertEqual(terminal.attempt_id, attempt.attempt_id)

    def test_interrupted_claim_terminalizes_before_live_inventory_validation(self) -> None:
        assignment = self.plan.assignments[0]
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        snapshot_root = self.runtime_artifact_root / "source-snapshots"
        snapshot_root.mkdir(mode=0o700)
        snapshot_path = self._retained_snapshot_path(self.runtime_artifact_root)
        retained_bytes = b"interrupted-private-snapshot"
        snapshot_path.write_bytes(retained_bytes)
        snapshot_path.chmod(0o400)
        write_av1_validation_derivation_assignment_claim(
            attempts_dir,
            assignment_id=assignment.assignment_id,
            plan_id=self.plan.plan_id,
            authorization_id=self.plan.authorization.authorization_id,
            claimed_at="2026-07-28T01:00:00Z",
        )
        drifted_config = SimpleNamespace(
            paths=SimpleNamespace(
                db_path=self.runtime_config.paths.db_path.with_name("drifted.sqlite3"),
                review_dir=self.runtime_config.paths.review_dir.with_name("drifted-review"),
                web_state_dir=self.runtime_config.paths.web_state_dir,
            )
        )
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_readonly_db",
                side_effect=AssertionError("live inventory must not be read"),
            ) as open_database,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract",
                side_effect=AssertionError("execution drift must not be checked"),
            ) as execution_contract,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "interrupted state was terminalized",
            ),
        ):
            _run_av1_validation_derivation_assignment_locked(
                config=drifted_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=self._matching_repository_identity,
                assignment_id=assignment.assignment_id,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                now_iso=lambda: "2026-07-28T01:01:00Z",
            )
        open_database.assert_not_called()
        execution_contract.assert_not_called()
        attempt = load_av1_validation_derivation_attempts(attempts_dir)[0]
        terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
        self.assertEqual(attempt.reason_code, "interrupted_claim")
        self.assertEqual(terminal.attempt_id, attempt.attempt_id)
        self.assertEqual(snapshot_path.read_bytes(), retained_bytes)

    def test_source_integrity_probe_fails_before_assignment_claim(self) -> None:
        assignment = self.plan.assignments[0]
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract"
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_readonly_db",
                return_value=nullcontext(object()),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory",
                return_value=SimpleNamespace(
                    sources=self.sources,
                    expectations=self.expectations,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.probe_macos_file_integrity",
                side_effect=FileIntegrityError("probe failed"),
            ) as integrity_probe,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "probe failed before assignment claim",
            ),
        ):
            _run_av1_validation_derivation_assignment_locked(
                config=self.runtime_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=self._matching_repository_identity,
                assignment_id=assignment.assignment_id,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                now_iso=lambda: "2026-07-29T01:00:00Z",
            )
        integrity_probe.assert_called_once_with(self.runtime_artifact_root.resolve())
        self.assertFalse(attempts_dir.exists())

    def test_assignment_expiry_after_claim_stops_before_database_or_media(self) -> None:
        assignment = self.plan.assignments[0]
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        timestamps = iter((
            "2026-07-31T23:59:57Z",
            "2026-07-31T23:59:58Z",
            "2026-07-31T23:59:58Z",
            "2026-07-31T23:59:59Z",
            VALID_UNTIL,
        ))

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract"
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_readonly_db",
                return_value=nullcontext(object()),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory",
                return_value=SimpleNamespace(
                    sources=self.sources,
                    expectations=self.expectations,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_db",
            ) as open_database,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._pinned_derivation_source",
            ) as pinned_source,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.run_sampled_calibration",
            ) as calibration,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.purge_transient_artifacts",
            ),
        ):
            attempt = _run_av1_validation_derivation_assignment_locked(
                config=self.runtime_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=self._matching_repository_identity,
                assignment_id=assignment.assignment_id,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                now_iso=lambda: next(timestamps),
            )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.reason_code, "authorization_expired")
        open_database.assert_not_called()
        pinned_source.assert_not_called()
        calibration.assert_not_called()
        terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
        self.assertEqual(terminal.reason_code, "authorization_expired")

    def test_final_execution_contract_expiry_is_authorization_expired(self) -> None:
        assignment = self.plan.assignments[0]
        source = next(
            item
            for item in self.partition.inventory_sources
            if item.local_item_id == assignment.local_item_id
        )
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        sample_item = {
            "library_item_id": assignment.local_item_id,
            "source_size_bytes": SOURCE_SIZE_BYTES,
            "resolved_policy": {},
        }
        source_commitment = av1_validation_derivation_plan_source_commitment(
            self.plan,
            assignment.assignment_id,
        )
        pinned_source = SimpleNamespace(
            path=self.runtime_artifact_root / "source-snapshots" / "source.mkv",
            content_sha256=source_commitment.source_sha256,
            size_bytes=source_commitment.source_size_bytes,
            content_version_fingerprint=source.source_identity,
        )
        deadline_expired = ProcessDeadlineExpiredError(
            "authorization deadline expired during final contract check"
        )

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract",
                side_effect=(None, deadline_expired),
            ) as execution_contract,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_readonly_db",
                return_value=nullcontext(object()),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory",
                return_value=SimpleNamespace(
                    sources=self.sources,
                    expectations=self.expectations,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                return_value=SimpleNamespace(free=100 * 1024 ** 3),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.purge_transient_artifacts",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_db",
                return_value=nullcontext(SimpleNamespace()),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_sample_item",
                return_value=sample_item,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._bind_derivation_intent",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._validate_bound_sample_item",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.snapshot_staged_artifact",
                return_value=None,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.restore_staged_artifact",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.resolve_item_source_path",
                return_value=Path("/private/source.mkv"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._derivation_prefix",
                return_value="private/derivation",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._pinned_derivation_source",
                return_value=nullcontext(pinned_source),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.run_sampled_calibration",
                return_value=({"review_artifact_fingerprint": "unused"}, None),
            ),
        ):
            attempt = _run_av1_validation_derivation_assignment_locked(
                config=self.runtime_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=self._matching_repository_identity,
                assignment_id=assignment.assignment_id,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                now_iso=lambda: "2026-07-31T23:59:59Z",
            )

        self.assertEqual(execution_contract.call_count, 2)
        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.reason_code, "authorization_expired")
        terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.reason_code, "authorization_expired")

    def test_execution_rechecks_live_source_commitments_before_artifact_writes(self) -> None:
        assignment = self.plan.assignments[0]
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        events: list[str] = []

        @contextmanager
        def source_sha256_resolver_context(
                *_args: object,
                **_kwargs: object,
        ) -> Iterator[object]:
            session = _SourceSHA256Session(
                on_verify=lambda: events.append("source-verify"),
                on_quiet=lambda: events.append("source-quiet"),
            )
            yield session
            session.assert_quiet()

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_readonly_db",
                return_value=nullcontext(object()),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory",
                return_value=SimpleNamespace(
                    sources=self.sources,
                    expectations=self.expectations,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.av1_validation_partition_source_sha256_resolver",
                side_effect=source_sha256_resolver_context,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                return_value=SimpleNamespace(free=0),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.purge_transient_artifacts",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.write_av1_validation_derivation_assignment_claim",
                side_effect=lambda *_args, **_kwargs: events.append("assignment-claim"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.write_av1_validation_derivation_attempt",
                side_effect=lambda *_args, **_kwargs: events.append("attempt"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.write_av1_validation_derivation_terminal_record",
                side_effect=lambda *_args, **_kwargs: events.append("terminal-record"),
            ),
        ):
            attempt = _run_av1_validation_derivation_assignment_locked(
                config=self.runtime_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=self._matching_repository_identity,
                assignment_id=assignment.assignment_id,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                now_iso=lambda: "2026-07-30T01:00:00Z",
            )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.reason_code, "storage_stop")
        self.assertEqual(events, [
            "source-verify",
            "source-quiet",
            "assignment-claim",
            "source-quiet",
            "attempt",
            "source-quiet",
            "terminal-record",
            "source-quiet",
        ])

    def test_repository_snapshot_drift_before_media_fails_closed(self) -> None:
        calibration_ran = self._run_assignment_with_repository_drift(
            drift_phase="before_media",
        )

        self.assertFalse(calibration_ran)

    def test_repository_snapshot_drift_after_media_blocks_attempt_publication(self) -> None:
        calibration_ran = self._run_assignment_with_repository_drift(
            drift_phase="before_publication",
        )

        self.assertTrue(calibration_ran)

    def test_assignment_review_identity_drift_is_safety_stop(self) -> None:
        assignment = self.plan.assignments[0]
        source = next(
            item
            for item in self.partition.inventory_sources
            if item.local_item_id == assignment.local_item_id
        )
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        sample_item = {
            "library_item_id": assignment.local_item_id,
            "source_size_bytes": SOURCE_SIZE_BYTES,
            "resolved_policy": {},
        }
        source_commitment = av1_validation_derivation_plan_source_commitment(
            self.plan,
            assignment.assignment_id,
        )
        pinned_source = SimpleNamespace(
            path=self.runtime_artifact_root / "source-snapshots" / "source.mkv",
            content_sha256=source_commitment.source_sha256,
            size_bytes=source_commitment.source_size_bytes,
            content_version_fingerprint=source.source_identity,
        )

        def run_calibration(**kwargs: object) -> tuple[dict[str, object], None]:
            secure_review_artifacts = kwargs["deps"].secure_review_artifacts
            self.assertIsNotNone(secure_review_artifacts)
            fingerprint = secure_review_artifacts(
                [SimpleNamespace(
                    output_path=Path("/private/preview.mp4"),
                    timestamp_seconds=0.0,
                    duration_seconds=8.0,
                )],
                [SimpleNamespace(
                    output_path=Path("/private/source.mp4"),
                    timestamp_seconds=0.0,
                    duration_seconds=8.0,
                )],
                [SimpleNamespace(
                    output_path=Path("/private/compare.mp4"),
                    timestamp_seconds=0.0,
                    duration_seconds=8.0,
                )],
            )
            return {"review_artifact_fingerprint": fingerprint}, None

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_readonly_db",
                return_value=nullcontext(object()),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory",
                return_value=SimpleNamespace(
                    sources=self.sources,
                    expectations=self.expectations,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                return_value=SimpleNamespace(free=100 * 1024 ** 3),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.purge_transient_artifacts",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_db",
                return_value=nullcontext(SimpleNamespace()),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_sample_item",
                return_value=sample_item,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._bind_derivation_intent",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._validate_bound_sample_item",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.snapshot_staged_artifact",
                return_value=None,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.restore_staged_artifact",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.resolve_item_source_path",
                return_value=Path("/private/source.mkv"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._derivation_prefix",
                return_value="private/derivation",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._pinned_derivation_source",
                return_value=nullcontext(pinned_source),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.run_sampled_calibration",
                side_effect=run_calibration,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._secure_and_fingerprint_derivation_review_clips",
                return_value="cira3_expected",
            ) as secure_and_fingerprint,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                return_value="cira3_changed",
            ),
        ):
            attempt = _run_av1_validation_derivation_assignment_locked(
                config=self.runtime_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=self._matching_repository_identity,
                assignment_id=assignment.assignment_id,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                now_iso=lambda: "2026-07-29T01:00:00Z",
            )
        self.assertEqual(attempt.status, "stopped")
        self.assertEqual(attempt.reason_code, "safety_stop")
        terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        secured_roles = {
            clip.role
            for clip in secure_and_fingerprint.call_args.kwargs["clips"]
        }
        self.assertEqual(secured_roles, {"preview", "source", "compare"})

    def test_fresh_authorization_timestamp_is_sampled_after_preflight(self) -> None:
        assignment = self.plan.assignments[0]
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        timestamps = iter(("2026-07-31T23:59:59Z", VALID_UNTIL))
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract"
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_readonly_db",
                return_value=nullcontext(object()),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory",
                return_value=SimpleNamespace(
                    sources=self.sources,
                    expectations=self.expectations,
                ),
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "outside its authorization window",
            ),
        ):
            _run_av1_validation_derivation_assignment_locked(
                config=self.runtime_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=self._matching_repository_identity,
                assignment_id=assignment.assignment_id,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                now_iso=lambda: next(timestamps),
            )
        self.assertFalse(attempts_dir.exists())

    def test_runtime_lock_path_canonicalizes_web_state_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "real" / "state"
            state_dir.mkdir(parents=True)
            alias_parent = root / "alias"
            alias_parent.mkdir()
            alias_state = alias_parent / "state"
            alias_state.symlink_to(state_dir, target_is_directory=True)
            real_config = SimpleNamespace(
                paths=SimpleNamespace(web_state_dir=state_dir)
            )
            alias_config = SimpleNamespace(
                paths=SimpleNamespace(web_state_dir=alias_state)
            )
            self.assertEqual(
                mediaforce_runtime_lock_path(real_config),
                mediaforce_runtime_lock_path(alias_config),
            )

    def test_runtime_context_binds_database_inode(self) -> None:
        original_context = av1_validation_derivation_runtime_context_sha256(
            self.runtime_config
        )
        db_path = self.runtime_config.paths.db_path
        replacement_path = db_path.with_name("replacement.sqlite3")
        replacement_path.write_bytes(db_path.read_bytes())
        replacement_path.chmod(0o600)
        replacement_path.replace(db_path)

        self.assertNotEqual(
            av1_validation_derivation_runtime_context_sha256(
                self.runtime_config
            ),
            original_context,
        )

    def test_pinned_source_snapshot_survives_original_path_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            original_bytes = b"original-source" * 20_000
            source_path.write_bytes(original_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(original_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(original_bytes),
                    process_controller=ManagedProcessController(),
                ) as pinned_source,
            ):
                moved_source = root / "moved-source.mkv"
                source_path.rename(moved_source)
                source_path.write_bytes(b"replacement-source" * 20_000)
                self.assertEqual(pinned_source.path.read_bytes(), original_bytes)
                self.assertEqual(
                    pinned_source.content_version_fingerprint,
                    source_identity,
                )
                self.assertEqual(pinned_source.size_bytes, len(original_bytes))
                self.assertTrue(pinned_source.content_sha256.startswith("sha256:"))
                self.assertEqual(pinned_source.path.stat().st_mode & 0o777, 0o400)
                self.assertEqual(pinned_source.path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                self._retained_snapshot_path(artifact_root).read_bytes(),
                original_bytes,
            )

    def test_retained_snapshot_existing_assignment_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            snapshot_root = artifact_root / "source-snapshots"
            snapshot_root.mkdir(mode=0o700)
            snapshot_path = self._retained_snapshot_path(artifact_root)
            existing_bytes = b"existing-private-residue"
            snapshot_path.write_bytes(existing_bytes)
            snapshot_path.chmod(0o400)
            existing_mode = snapshot_path.stat().st_mode & 0o777
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 20_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )

            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "retained source snapshot already exists",
                ),
            ):
                with _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ):
                    pass

            self.assertEqual(snapshot_path.read_bytes(), existing_bytes)
            self.assertEqual(snapshot_path.stat().st_mode & 0o777, existing_mode)

    def test_retained_snapshot_rejects_invalid_assignment_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 20_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )

            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "assignment ID is invalid for retained snapshot storage",
            ):
                with _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id="../outside",
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ):
                    pass

            self.assertFalse((artifact_root / "source-snapshots").exists())

    def test_retained_snapshot_lifecycle_never_deletes_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 20_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )

            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.os.unlink",
                    side_effect=AssertionError("snapshot lifecycle must not unlink"),
                ),
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.os.rmdir",
                    side_effect=AssertionError("snapshot lifecycle must not rmdir"),
                ),
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.rmtree",
                    side_effect=AssertionError("snapshot lifecycle must not rmtree"),
                ),
                _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ),
            ):
                pass

            self.assertEqual(
                self._retained_snapshot_path(artifact_root).read_bytes(),
                source_bytes,
            )
            self.assertEqual(
                self._retained_snapshot_path(artifact_root).stat().st_mode & 0o777,
                0o400,
            )

    def test_retained_snapshot_reopen_substitution_preserves_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 20_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            snapshot_path = self._retained_snapshot_path(artifact_root)
            moved_snapshot_path = root / "moved-source-snapshot"
            replacement_bytes = b"outside-replacement"
            original_open = os.open
            snapshot_open_count = 0

            def open_with_snapshot_substitution(
                    path: os.PathLike[str] | str,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
            ) -> int:
                nonlocal snapshot_open_count
                candidate = Path(path)
                is_snapshot = (
                    dir_fd is not None
                    and candidate.name.endswith(".source-media")
                )
                if is_snapshot:
                    snapshot_open_count += 1
                    if snapshot_open_count == 2:
                        snapshot_path.rename(moved_snapshot_path)
                        snapshot_path.write_bytes(replacement_bytes)
                        snapshot_path.chmod(0o400)
                if dir_fd is None:
                    return original_open(path, flags, mode)
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.os.open",
                    side_effect=open_with_snapshot_substitution,
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "source snapshot changed before monitoring",
                ),
            ):
                with _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ):
                    pass

            self.assertEqual(snapshot_open_count, 2)
            self.assertEqual(snapshot_path.read_bytes(), replacement_bytes)
            self.assertEqual(snapshot_path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(moved_snapshot_path.read_bytes(), source_bytes)

    def test_pinned_source_snapshot_rejects_full_digest_mismatch_before_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 20_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            media_entered = False
            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "source changed while its snapshot was created",
                ),
            ):
                with _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=f"sha256:{'f' * 64}",
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ):
                    media_entered = True
            self.assertFalse(media_entered)
            self.assertEqual(
                self._retained_snapshot_path(artifact_root).read_bytes(),
                source_bytes,
            )

    def test_pinned_source_snapshot_rejects_swap_before_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            registered_bytes = b"registered-source" * 20_000
            source_path.write_bytes(registered_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            replacement_bytes = b"unreserved-source" * 20_000
            source_path.write_bytes(replacement_bytes)
            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "source bytes drifted from the frozen reservation",
                ),
            ):
                with _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(registered_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(replacement_bytes),
                    process_controller=ManagedProcessController(),
                ):
                    pass
            self.assertFalse((artifact_root / "source-snapshots").exists())

    def test_pinned_source_snapshot_detects_private_snapshot_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 20_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "pinned source changed during media execution",
                ),
            ):
                with _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ) as pinned_source:
                    pinned_source.path.parent.chmod(0o700)
                    pinned_source.path.chmod(0o600)
                    pinned_source.path.write_bytes(b"mutated-source")
            self.assertTrue(self._retained_snapshot_path(artifact_root).exists())

    @unittest.skipUnless(hasattr(__import__("select"), "kqueue"), "requires kqueue")
    def test_pinned_source_snapshot_detects_transient_write_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 20_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.MacOSFileIntegrityGuard",
                    new=MacOSFileIntegrityGuard,
                ),
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "pinned source changed during media execution",
                ),
            ):
                with _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ) as pinned_source:
                    pinned_source.path.chmod(0o600)
                    pinned_source.path.write_bytes(b"transient-source")
                    pinned_source.path.write_bytes(source_bytes)
                    pinned_source.path.chmod(0o400)
            self.assertEqual(
                self._retained_snapshot_path(artifact_root).read_bytes(),
                source_bytes,
            )

    @unittest.skipUnless(hasattr(__import__("select"), "kqueue"), "requires kqueue")
    def test_pinned_source_snapshot_detects_hardlink_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 20_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            link_path = root / "snapshot-alias"
            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.MacOSFileIntegrityGuard",
                    new=MacOSFileIntegrityGuard,
                ),
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "pinned source changed during media execution",
                ),
            ):
                with _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ) as pinned_source:
                    os.link(pinned_source.path, link_path)
                    link_path.unlink()
            self.assertFalse(link_path.exists())
            self.assertEqual(
                self._retained_snapshot_path(artifact_root).read_bytes(),
                source_bytes,
            )

    @unittest.skipUnless(hasattr(__import__("select"), "kqueue"), "requires kqueue")
    def test_pinned_source_snapshot_detects_parent_swap_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 20_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.MacOSFileIntegrityGuard",
                    new=MacOSFileIntegrityGuard,
                ),
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "pinned source changed during media execution",
                ),
            ):
                with _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ) as pinned_source:
                    assignment_root = pinned_source.path.parent
                    moved_root = assignment_root.with_name(
                        f"{assignment_root.name}-moved"
                    )
                    assignment_root.rename(moved_root)
                    try:
                        assignment_root.mkdir(mode=0o700)
                        replacement_path = assignment_root / pinned_source.path.name
                        replacement_path.write_bytes(b"replacement-source")
                        replacement_path.chmod(0o400)
                        assignment_root.chmod(0o500)
                        self.assertEqual(
                            pinned_source.path.read_bytes(),
                            b"replacement-source",
                        )
                    finally:
                        if assignment_root.exists():
                            assignment_root.chmod(0o700)
                            replacement_path = assignment_root / pinned_source.path.name
                            if replacement_path.exists():
                                replacement_path.chmod(0o600)
                                replacement_path.unlink()
                            assignment_root.rmdir()
                        moved_root.rename(assignment_root)
            self.assertEqual(
                self._retained_snapshot_path(artifact_root).read_bytes(),
                source_bytes,
            )

    @unittest.skipUnless(hasattr(__import__("select"), "kqueue"), "requires kqueue")
    def test_pinned_source_snapshot_guard_runs_when_body_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 20_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.MacOSFileIntegrityGuard",
                    new=MacOSFileIntegrityGuard,
                ),
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "pinned source changed during media execution",
                ),
            ):
                with _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ) as pinned_source:
                    pinned_source.path.chmod(0o600)
                    pinned_source.path.write_bytes(b"transient-source")
                    pinned_source.path.write_bytes(source_bytes)
                    pinned_source.path.chmod(0o400)
                    raise RuntimeError("media body failed")

    def test_pinned_source_snapshot_uses_read_only_monitored_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 20_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            access_modes: list[int] = []
            original_guard = _DescriptorBindingFileIntegrityGuard

            def guard_factory(**kwargs: object) -> _DescriptorBindingFileIntegrityGuard:
                descriptor = int(kwargs["descriptor"])
                access_modes.append(
                    fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
                )
                return original_guard(**kwargs)

            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.MacOSFileIntegrityGuard",
                    side_effect=guard_factory,
                ),
                _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ),
            ):
                pass
            self.assertEqual(access_modes, [os.O_RDONLY])

    def test_pinned_source_snapshot_reopen_failure_does_not_double_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 20_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            original_open = os.open
            original_close = os.close
            snapshot_descriptor = -1
            snapshot_open_count = 0
            live_descriptors: set[int] = set()
            invalid_closes: list[int] = []

            def open_with_reopen_failure(
                    path: os.PathLike[str] | str,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
            ) -> int:
                nonlocal snapshot_descriptor, snapshot_open_count
                candidate = Path(path)
                is_snapshot = (
                    dir_fd is not None
                    and candidate.name.endswith(".source-media")
                )
                if is_snapshot:
                    snapshot_open_count += 1
                    if snapshot_open_count == 2:
                        raise OSError("fixture snapshot reopen failure")
                if dir_fd is None:
                    descriptor = original_open(path, flags, mode)
                else:
                    descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                live_descriptors.add(descriptor)
                if is_snapshot and snapshot_open_count == 1:
                    snapshot_descriptor = descriptor
                return descriptor

            def tracked_close(descriptor: int) -> None:
                if descriptor not in live_descriptors:
                    invalid_closes.append(descriptor)
                else:
                    live_descriptors.remove(descriptor)
                original_close(descriptor)

            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.os.open",
                    side_effect=open_with_reopen_failure,
                ),
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.os.close",
                    side_effect=tracked_close,
                ),
                self.assertRaisesRegex(OSError, "fixture snapshot reopen failure"),
            ):
                with _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ):
                    pass
            self.assertGreaterEqual(snapshot_descriptor, 0)
            self.assertEqual(invalid_closes, [])
            self.assertNotIn(snapshot_descriptor, live_descriptors)
            self.assertEqual(live_descriptors, set())
            self.assertTrue(self._retained_snapshot_path(artifact_root).exists())

    def test_pinned_source_snapshot_rejects_mutation_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            source_path = root / "source.mkv"
            source_bytes = b"registered-source" * 400_000
            source_path.write_bytes(source_bytes)
            source_stat = source_path.stat()
            source_identity = content_version_fingerprint(source_path, source_stat)
            mutation_injected = False
            snapshot_modes_during_copy: list[int] = []

            def write_and_mutate(descriptor: int, payload: bytes) -> None:
                nonlocal mutation_injected
                snapshot_modes_during_copy.append(
                    os.fstat(descriptor).st_mode & 0o777
                )
                _write_all(descriptor, payload)
                if mutation_injected:
                    return
                mutation_injected = True
                with source_path.open("r+b") as source_handle:
                    source_handle.seek(1024 * 1024)
                    source_handle.write(b"X")
                os.utime(
                    source_path,
                    ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 1_000_000_000),
                )

            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=100 * 1024 ** 3),
                ),
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation._write_all",
                    side_effect=write_and_mutate,
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "source changed while its snapshot was created",
                ),
            ):
                with _pinned_derivation_source(
                    artifact_root=artifact_root,
                    assignment_id=self.plan.assignments[0].assignment_id,
                    source_path=source_path,
                    expected_content_version_fingerprint=source_identity,
                    expected_source_sha256=(
                        f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
                    ),
                    expected_size_bytes=len(source_bytes),
                    process_controller=ManagedProcessController(),
                ):
                    pass
            self.assertTrue(mutation_injected)
            self.assertEqual(set(snapshot_modes_during_copy), {0o400})
            retained_snapshot = self._retained_snapshot_path(artifact_root)
            self.assertTrue(retained_snapshot.exists())
            self.assertEqual(retained_snapshot.stat().st_mode & 0o777, 0o400)

    def test_assignment_loader_allows_recovery_before_full_runtime_context_check(self) -> None:
        drifted_config = SimpleNamespace(
            paths=SimpleNamespace(
                db_path=self.runtime_config.paths.db_path.with_name("drifted.sqlite3"),
                review_dir=self.runtime_config.paths.review_dir.with_name("drifted-review"),
                web_state_dir=self.runtime_config.paths.web_state_dir,
            )
        )
        with patch.object(
            verify_av1_cold_start_preregistration,
            "load_config",
            return_value=drifted_config,
        ):
            plan, artifact_root = (
                verify_av1_cold_start_preregistration._load_recovery_capable_derivation_plan(
                    plan_path=self.runtime_artifact_root / "plan.json",
                    config_path=Path("unused.toml"),
                )
            )
        self.assertEqual(plan, self.plan)
        self.assertEqual(artifact_root, self.runtime_artifact_root.resolve())

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
                artifact_root=root,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                completed_at="2026-07-28T01:01:00Z",
            ))
            terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
            self.assertEqual(terminal.attempt_id, attempt.attempt_id)

    def test_interrupted_verdict_claim_is_recovered_as_safety_stop(self) -> None:
        attempt = self._review_pending_attempt()
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        write_av1_validation_derivation_attempt(attempts_dir, attempt)
        self.assertTrue(ensure_av1_validation_derivation_verdict_claim(
            self.runtime_artifact_root / "verdict-claims",
            plan=self.plan,
            attempt=attempt,
            claimed_at="2026-07-28T01:06:00Z",
        ))

        self.assertTrue(_recover_interrupted_derivation_state(
            plan=self.plan,
            partition=self.partition,
            artifact_root=self.runtime_artifact_root,
            attempts_directory=attempts_dir,
            terminal_records_directory=records_dir,
            completed_at="2026-07-28T01:07:00Z",
        ))

        terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        self.assertEqual(terminal.attempt_id, attempt.attempt_id)

    def test_frozen_verdict_intent_remains_retryable_after_interruption(self) -> None:
        attempt = self._review_pending_attempt()
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        write_av1_validation_derivation_attempt(attempts_dir, attempt)
        ensure_av1_validation_derivation_verdict_claim(
            self.runtime_artifact_root / "verdict-claims",
            plan=self.plan,
            attempt=attempt,
            claimed_at="2026-07-28T01:06:00Z",
        )
        resolve_av1_validation_derivation_verdict_intent(
            self.runtime_artifact_root / "verdict-intents",
            plan=self.plan,
            attempt=attempt,
            verdict="approved",
            concern_tags=[],
            evidence_ids=[],
            moment_indexes=[],
            recorded_at="2026-07-28T01:06:00Z",
        )

        self.assertFalse(_recover_interrupted_derivation_state(
            plan=self.plan,
            partition=self.partition,
            artifact_root=self.runtime_artifact_root,
            attempts_directory=attempts_dir,
            terminal_records_directory=records_dir,
            completed_at="2026-07-28T01:07:00Z",
        ))
        self.assertFalse(records_dir.exists())

    def test_interrupted_terminal_publication_is_completed_from_intent(self) -> None:
        attempt = self._review_pending_attempt()
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        write_av1_validation_derivation_attempt(attempts_dir, attempt)
        ensure_av1_validation_derivation_verdict_claim(
            self.runtime_artifact_root / "verdict-claims",
            plan=self.plan,
            attempt=attempt,
            claimed_at="2026-07-28T01:06:00Z",
        )
        resolve_av1_validation_derivation_verdict_intent(
            self.runtime_artifact_root / "verdict-intents",
            plan=self.plan,
            attempt=attempt,
            verdict="approved",
            concern_tags=[],
            evidence_ids=[],
            moment_indexes=[],
            recorded_at="2026-07-28T01:06:00Z",
        )
        terminal = build_av1_validation_derivation_terminal_record(
            plan=self.plan,
            partition=self.partition,
            attempt=attempt,
            review_failure_reason_code="safety_stop",
        )
        ensure_av1_validation_derivation_terminal_intent(
            self.runtime_artifact_root / "terminal-intents",
            terminal,
        )

        self.assertTrue(_recover_interrupted_derivation_state(
            plan=self.plan,
            partition=self.partition,
            artifact_root=self.runtime_artifact_root,
            attempts_directory=attempts_dir,
            terminal_records_directory=records_dir,
            completed_at="2026-07-28T01:07:00Z",
        ))
        self.assertEqual(
            load_av1_validation_derivation_terminal_records(records_dir),
            (terminal,),
        )

    def test_recovered_observed_terminal_allows_frozen_verdict_retry(self) -> None:
        assignment = self.plan.assignments[0]
        attempt = self._review_pending_attempt()
        observation = _observation(
            assignment=assignment,
            source_identity=_source_identity(self.partition, assignment),
            crf=28.0,
            bitrate=1_000_000,
            verdict="acceptable",
        )
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        write_av1_validation_derivation_attempt(attempts_dir, attempt)
        ensure_av1_validation_derivation_verdict_claim(
            self.runtime_artifact_root / "verdict-claims",
            plan=self.plan,
            attempt=attempt,
            claimed_at="2026-07-28T01:06:00Z",
        )
        resolve_av1_validation_derivation_verdict_intent(
            self.runtime_artifact_root / "verdict-intents",
            plan=self.plan,
            attempt=attempt,
            verdict="approved",
            concern_tags=[],
            evidence_ids=[],
            moment_indexes=[],
            recorded_at="2026-07-28T01:06:00Z",
        )
        terminal = build_av1_validation_derivation_terminal_record(
            plan=self.plan,
            partition=self.partition,
            attempt=attempt,
            observation=observation,
        )
        ensure_av1_validation_derivation_terminal_intent(
            self.runtime_artifact_root / "terminal-intents",
            terminal,
        )
        self.assertTrue(_recover_interrupted_derivation_state(
            plan=self.plan,
            partition=self.partition,
            artifact_root=self.runtime_artifact_root,
            attempts_directory=attempts_dir,
            terminal_records_directory=records_dir,
            completed_at="2026-07-28T01:07:00Z",
        ))
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_current_content_intent_boundary_observations",
                return_value=[],
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "idempotent verdict retry",
            ),
        ):
            _assert_derivation_terminal_observations_current(
                connection=SimpleNamespace(),
                records=(terminal,),
            )

        @contextmanager
        def database(
                _path: Path,
                **_kwargs: object,
        ) -> Iterator[SimpleNamespace]:
            yield SimpleNamespace(exec_driver_sql=lambda _sql: None)

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
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                return_value=attempt.calibration_payload()["review_artifact_fingerprint"],
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
                "mediaforce.web.runtime.av1_validation_derivation.append_content_intent_boundary_observation"
            ) as append_observation,
        ):
            retried_terminal = record_av1_validation_derivation_visual_verdict(
                config_path=Path("unused.toml"),
                manifest=self.manifest,
                plan=self.plan,
                partition=self.partition,
                token_key=self.token_key,
                attempt=attempt,
                terminal_records_directory=records_dir,
                verdict="approved",
                concern_tags=[],
                evidence_ids=[],
                moment_indexes=[],
                recorded_at="2026-07-28T01:06:00Z",
            )
        self.assertEqual(retried_terminal, terminal)
        append_observation.assert_called_once()
        current_observation = self._current_observations([terminal])[
            terminal.assignment_id
        ]
        with patch(
            "mediaforce.web.runtime.av1_validation_derivation.load_current_content_intent_boundary_observations",
            return_value=[current_observation.values()],
        ):
            _assert_derivation_terminal_observations_current(
                connection=SimpleNamespace(),
                records=(terminal,),
            )

    def test_frozen_verdict_retry_mismatch_is_retryable(self) -> None:
        attempt = self._review_pending_attempt()
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        write_av1_validation_derivation_attempt(attempts_dir, attempt)
        ensure_av1_validation_derivation_verdict_claim(
            self.runtime_artifact_root / "verdict-claims",
            plan=self.plan,
            attempt=attempt,
            claimed_at="2026-07-28T01:06:00Z",
        )
        resolve_av1_validation_derivation_verdict_intent(
            self.runtime_artifact_root / "verdict-intents",
            plan=self.plan,
            attempt=attempt,
            verdict="approved",
            concern_tags=["banding"],
            evidence_ids=["evidence_test"],
            moment_indexes=[1],
            recorded_at="2026-07-28T01:06:00Z",
        )

        @contextmanager
        def database(
                _path: Path,
                **_kwargs: object,
        ) -> Iterator[SimpleNamespace]:
            yield SimpleNamespace(exec_driver_sql=lambda _sql: None)

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
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                return_value=attempt.calibration_payload()["review_artifact_fingerprint"],
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationVerdictRetryMismatchError,
                "does not match its immutable intent",
            ) as retry_error,
        ):
            record_av1_validation_derivation_visual_verdict(
                config_path=Path("unused.toml"),
                manifest=self.manifest,
                plan=self.plan,
                partition=self.partition,
                token_key=self.token_key,
                attempt=attempt,
                terminal_records_directory=records_dir,
                verdict="rejected",
                concern_tags=[],
                evidence_ids=[],
                moment_indexes=[],
                recorded_at="2026-07-28T01:06:00Z",
            )

        self.assertFalse(records_dir.exists())
        self.assertEqual(
            {
                "concern_tags": retry_error.exception.frozen_intent[
                    "concern_tags"
                ],
                "evidence_ids": retry_error.exception.frozen_intent[
                    "evidence_ids"
                ],
                "moment_indexes": retry_error.exception.frozen_intent[
                    "moment_indexes"
                ],
                "verdict": retry_error.exception.frozen_intent["verdict"],
            },
            {
                "concern_tags": ["banding"],
                "evidence_ids": ["evidence_test"],
                "moment_indexes": [1],
                "verdict": "approved",
            },
        )
        self.assertIn(
            'retry with {"concern_tags":["banding"],'
            '"evidence_ids":["evidence_test"],"moment_indexes":[1],'
            '"verdict":"approved"}',
            str(retry_error.exception),
        )

    def test_observed_terminal_blocks_progress_until_database_commit_is_present(self) -> None:
        record = self._observed_record(
            self.plan.assignments[0].assignment_id,
            crf=28.0,
        )
        current_observation = self._current_observations([record])[
            record.assignment_id
        ]
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_current_content_intent_boundary_observations",
                return_value=[],
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "idempotent verdict retry",
            ),
        ):
            _assert_derivation_terminal_observations_current(
                connection=SimpleNamespace(),
                records=(record,),
            )
        with patch(
            "mediaforce.web.runtime.av1_validation_derivation.load_current_content_intent_boundary_observations",
            return_value=[current_observation.values()],
        ):
            _assert_derivation_terminal_observations_current(
                connection=SimpleNamespace(),
                records=(record,),
            )

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
                    repository_identity_resolver=self._matching_repository_identity,
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
            plan_path.chmod(0o400)
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
                            repository_commit=self.plan.repository_commit,
                            repository_tree=self.plan.repository_tree,
                        )

    def test_derivation_review_media_is_owner_only_and_no_follow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifact-root"
            artifact_root.mkdir(mode=0o700)
            review_root = _prepare_derivation_review_root(artifact_root)
            run_directory = review_root / "run"
            run_directory.mkdir(mode=0o700)
            clip_directory = run_directory / "item-00"
            clip_directory.mkdir(mode=0o700)
            clip_path = clip_directory / "encoded-01.mp4"
            clip_path.write_bytes(b"review-clip")
            clip_path.chmod(0o600)

            _secure_derivation_review_media(review_root)

            self.assertEqual(review_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(clip_directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(clip_path.stat().st_mode & 0o777, 0o400)
            calibration = {
                "preview_clips": [{
                    "path": clip_path.as_uri(),
                    "timestamp_seconds": 1.0,
                    "duration_seconds": 8.0,
                }],
                "source_clips": [],
            }
            fingerprint = _current_derivation_review_artifact_fingerprint(
                review_root=review_root,
                calibration=calibration,
            )
            self.assertIsNotNone(fingerprint)
            assert fingerprint is not None
            self.assertTrue(fingerprint.startswith("cira3_"))

            linked_clip = clip_directory / "linked.mp4"
            linked_clip.symlink_to(clip_path)
            calibration["preview_clips"][0]["path"] = linked_clip.as_uri()
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "must not contain links",
            ):
                _current_derivation_review_artifact_fingerprint(
                    review_root=review_root,
                    calibration=calibration,
                )
            linked_clip.unlink()

            clip_path.chmod(0o640)
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "must be owner-only",
            ):
                _secure_derivation_review_media(review_root)
            self.assertEqual(clip_path.stat().st_mode & 0o777, 0o640)
            clip_path.chmod(0o600)

            outside_path = artifact_root.parent / "outside-review-media"
            outside_path.write_bytes(b"outside-review-media")
            outside_path.chmod(0o640)
            outside_mode = outside_path.stat().st_mode & 0o777
            hardlink_path = clip_directory / "hardlinked.mp4"
            os.link(outside_path, hardlink_path)
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "must not contain hard links",
            ):
                _secure_derivation_review_media(review_root)
            self.assertEqual(outside_path.read_bytes(), b"outside-review-media")
            self.assertEqual(outside_path.stat().st_mode & 0o777, outside_mode)

    def test_review_fingerprint_binds_compare_clip_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifact-root"
            artifact_root.mkdir(mode=0o700)
            review_root = _prepare_derivation_review_root(artifact_root)
            clip_paths = {
                role: review_root / f"{role}.mp4"
                for role in ("preview", "source", "compare")
            }
            for role, clip_path in clip_paths.items():
                clip_path.write_bytes(f"{role}-review".encode())
                clip_path.chmod(0o400)
            calibration = {
                f"{role}_clips": [{
                    "path": clip_path.as_uri(),
                    "timestamp_seconds": 0.0,
                    "duration_seconds": 8.0,
                }]
                for role, clip_path in clip_paths.items()
            }
            first_fingerprint = _current_derivation_review_artifact_fingerprint(
                review_root=review_root,
                calibration=calibration,
            )
            compare_path = clip_paths["compare"]
            compare_path.chmod(0o600)
            compare_path.write_bytes(b"changed-compare-review")
            compare_path.chmod(0o400)
            second_fingerprint = _current_derivation_review_artifact_fingerprint(
                review_root=review_root,
                calibration=calibration,
            )
            self.assertIsNotNone(first_fingerprint)
            self.assertIsNotNone(second_fingerprint)
            self.assertNotEqual(first_fingerprint, second_fingerprint)

    def test_review_fingerprint_binds_matching_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifact-root"
            artifact_root.mkdir(mode=0o700)
            review_root = _prepare_derivation_review_root(artifact_root)
            run_directory = review_root / "run"
            run_directory.mkdir(mode=0o700)
            clip_path = run_directory / "encoded-01.mp4"
            clip_bytes = b"matching-review-clip"
            clip_path.write_bytes(clip_bytes)
            clip_path.chmod(0o600)
            moved_clip_path = run_directory / "original-encoded-01.mp4"
            calibration = {
                "preview_clips": [{
                    "path": clip_path.as_uri(),
                    "timestamp_seconds": 1.0,
                    "duration_seconds": 8.0,
                }],
                "source_clips": [],
            }

            original_fingerprint = _current_derivation_review_artifact_fingerprint(
                review_root=review_root,
                calibration=calibration,
            )
            self.assertIsNotNone(original_fingerprint)

            clip_path.rename(moved_clip_path)
            clip_path.write_bytes(clip_bytes)
            clip_path.chmod(0o400)
            replacement_fingerprint = _current_derivation_review_artifact_fingerprint(
                review_root=review_root,
                calibration=calibration,
            )
            self.assertIsNotNone(replacement_fingerprint)
            self.assertNotEqual(replacement_fingerprint, original_fingerprint)
            self.assertEqual(clip_path.read_bytes(), clip_bytes)
            self.assertEqual(moved_clip_path.read_bytes(), clip_bytes)

    @unittest.skipUnless(
        hasattr(__import__("select"), "kqueue"),
        "requires macOS kqueue",
    )
    def test_review_fingerprint_holds_all_real_guards_until_final_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifact-root"
            artifact_root.mkdir(mode=0o700)
            review_root = _prepare_derivation_review_root(artifact_root)
            run_directory = review_root / "run"
            run_directory.mkdir(mode=0o700)
            clip_paths = []
            for index in range(3):
                clip_path = run_directory / f"encoded-{index:02d}.mp4"
                clip_path.write_bytes(f"review-{index}".encode())
                clip_path.chmod(0o400)
                clip_paths.append(clip_path)
            calibration = {
                "preview_clips": [
                    {
                        "path": clip_path.as_uri(),
                        "timestamp_seconds": float(index),
                        "duration_seconds": 8.0,
                    }
                    for index, clip_path in enumerate(clip_paths)
                ],
                "source_clips": [],
            }
            active_guards = 0
            maximum_active_guards = 0

            class CountingGuard:
                def __init__(
                        self,
                        *,
                        path: Path,
                        descriptor: int,
                        require_single_link: bool,
                ) -> None:
                    nonlocal active_guards, maximum_active_guards
                    self._guard = MacOSFileIntegrityGuard(
                        path=path,
                        descriptor=descriptor,
                        require_single_link=require_single_link,
                    )
                    self._closed = False
                    active_guards += 1
                    maximum_active_guards = max(
                        maximum_active_guards,
                        active_guards,
                    )

                def assert_quiet(self, *, timeout_seconds: float = 0.0) -> None:
                    self._guard.assert_quiet(timeout_seconds=timeout_seconds)

                def close(self) -> None:
                    nonlocal active_guards
                    if not self._closed:
                        self._closed = True
                        self._guard.close()
                        active_guards -= 1

            with patch(
                "mediaforce.web.runtime.av1_validation_derivation.MacOSFileIntegrityGuard",
                new=CountingGuard,
            ):
                fingerprint = _current_derivation_review_artifact_fingerprint(
                    review_root=review_root,
                    calibration=calibration,
                )
            self.assertIsNotNone(fingerprint)
            self.assertEqual(maximum_active_guards, len(clip_paths))
            self.assertEqual(active_guards, 0)

    def test_review_fingerprint_resource_exhaustion_is_safety_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifact-root"
            artifact_root.mkdir(mode=0o700)
            review_root = _prepare_derivation_review_root(artifact_root)
            clip_path = review_root / "encoded-01.mp4"
            clip_path.write_bytes(b"review")
            clip_path.chmod(0o400)
            calibration = {
                "preview_clips": [{
                    "path": clip_path.as_uri(),
                    "timestamp_seconds": 0.0,
                    "duration_seconds": 8.0,
                }],
                "source_clips": [],
            }
            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation._open_owner_only_review_media_relative_file",
                    side_effect=OSError(errno.EMFILE, "too many open files"),
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "verification resources are unavailable",
                ),
            ):
                _current_derivation_review_artifact_fingerprint(
                    review_root=review_root,
                    calibration=calibration,
                )

    def test_review_fingerprint_reports_cleanup_failure_inside_outer_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifact-root"
            artifact_root.mkdir(mode=0o700)
            review_root = _prepare_derivation_review_root(artifact_root)
            clip_path = review_root / "encoded-01.mp4"
            clip_path.write_bytes(b"review")
            clip_path.chmod(0o400)
            calibration = {
                "preview_clips": [{
                    "path": clip_path.as_uri(),
                    "timestamp_seconds": 0.0,
                    "duration_seconds": 8.0,
                }],
                "source_clips": [],
                "compare_clips": [],
            }

            class CloseFailGuard:
                def __init__(self, **_kwargs: object) -> None:
                    pass

                def assert_quiet(self, *, timeout_seconds: float = 0.0) -> None:
                    pass

                def close(self) -> None:
                    raise OSError(errno.EIO, "guard close failed")

            try:
                raise RuntimeError("outer failure")
            except RuntimeError:
                with (
                    patch(
                        "mediaforce.web.runtime.av1_validation_derivation.MacOSFileIntegrityGuard",
                        new=CloseFailGuard,
                    ),
                    self.assertRaisesRegex(
                        AV1ValidationDerivationError,
                        "verification cleanup failed",
                    ),
                ):
                    _current_derivation_review_artifact_fingerprint(
                        review_root=review_root,
                        calibration=calibration,
                    )

    def test_review_fingerprint_rejects_symlinked_review_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_review_root = root / "real-review"
            real_review_root.mkdir(mode=0o700)
            clip_path = real_review_root / "encoded-01.mp4"
            clip_path.write_bytes(b"review")
            clip_path.chmod(0o400)
            symlinked_review_root = root / "review-media"
            symlinked_review_root.symlink_to(real_review_root, target_is_directory=True)
            calibration = {
                "preview_clips": [{
                    "path": clip_path.as_uri(),
                    "timestamp_seconds": 0.0,
                    "duration_seconds": 8.0,
                }],
                "source_clips": [],
            }
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "could not be safely bound",
            ):
                _current_derivation_review_artifact_fingerprint(
                    review_root=symlinked_review_root,
                    calibration=calibration,
                )

    def test_owner_only_umask_restores_previous_value(self) -> None:
        with patch(
            "mediaforce.web.runtime.av1_validation_derivation.os.umask",
            side_effect=(0o022, 0o077),
        ) as umask:
            with _owner_only_umask():
                pass
        self.assertEqual(
            umask.call_args_list,
            [call(0o077), call(0o022)],
        )

    def test_derivation_verdict_terminalizes_preexisting_claim_without_intent(self) -> None:
        attempt = self._review_pending_attempt()
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        ensure_av1_validation_derivation_verdict_claim(
            self.runtime_artifact_root / "verdict-claims",
            plan=self.plan,
            attempt=attempt,
            claimed_at="2026-07-28T01:06:00Z",
        )
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
                "mediaforce.web.runtime.av1_validation_derivation.open_db"
            ) as open_database,
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
                recorded_at="2026-07-28T01:07:00Z",
            )
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        open_database.assert_not_called()

    def test_derivation_verdict_terminalizes_database_open_failure(self) -> None:
        attempt = self._review_pending_attempt()
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
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
                side_effect=OSError("database unavailable"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint"
            ) as review_fingerprint,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.append_content_intent_boundary_observation"
            ) as append_observation,
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
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        review_fingerprint.assert_not_called()
        append_observation.assert_not_called()
        self.assertFalse((self.runtime_artifact_root / "verdict-intents").exists())

    def test_derivation_verdict_terminalizes_expired_first_verdict_after_claim(self) -> None:
        attempt = self._review_pending_attempt()
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
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
                "mediaforce.web.runtime.av1_validation_derivation.open_db"
            ) as open_database,
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
                recorded_at=VALID_UNTIL,
            )
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        open_database.assert_not_called()
        self.assertTrue(
            (
                self.runtime_artifact_root
                / "verdict-claims"
                / f"{attempt.assignment_id}.json"
            ).exists()
        )
        self.assertFalse((self.runtime_artifact_root / "verdict-intents").exists())

    def test_derivation_verdict_stops_when_claim_publication_crosses_expiry(
            self,
    ) -> None:
        attempt = self._review_pending_attempt()
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        clock_values = iter(("2026-07-31T23:59:59Z", VALID_UNTIL))
        clock_calls: list[str] = []

        def clock() -> str:
            value = next(clock_values)
            clock_calls.append(value)
            return value

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
                "mediaforce.web.runtime.av1_validation_derivation.open_db"
            ) as open_database,
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
                now_iso=clock,
            )
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        self.assertEqual(
            clock_calls,
            ["2026-07-31T23:59:59Z", VALID_UNTIL],
        )
        open_database.assert_not_called()
        self.assertFalse(
            (
                self.runtime_artifact_root
                / "verdict-claims"
                / f"{attempt.assignment_id}.json"
            ).exists()
        )
        self.assertFalse((self.runtime_artifact_root / "verdict-intents").exists())

    def test_derivation_verdict_terminalizes_transaction_begin_failure(self) -> None:
        attempt = self._review_pending_attempt()
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        connection = SimpleNamespace(
            exec_driver_sql=lambda _sql: (_ for _ in ()).throw(
                SQLAlchemyError("database is locked")
            )
        )
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
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_partition_inventory"
            ) as load_inventory,
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
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        load_inventory.assert_not_called()

    def test_derivation_verdict_terminalizes_inventory_load_failure(self) -> None:
        attempt = self._review_pending_attempt()
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
                side_effect=OSError("inventory unavailable"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.validate_av1_validation_partition_current_inputs"
            ) as validate_current_inputs,
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
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        validate_current_inputs.assert_not_called()

    def test_derivation_verdict_rolls_back_before_safety_terminal_publication(self) -> None:
        assignment = self.plan.assignments[0]
        attempt = self._review_pending_attempt()
        observation = _observation(
            assignment=assignment,
            source_identity=_source_identity(self.partition, assignment),
            crf=28.0,
            bitrate=1_000_000,
            verdict="acceptable",
        )
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        events: list[str] = []

        @contextmanager
        def database(
                _path: Path,
                **_kwargs: object,
        ) -> Iterator[SimpleNamespace]:
            events.append("db-enter")
            connection = SimpleNamespace(
                exec_driver_sql=lambda _sql: events.append("begin")
            )
            try:
                yield connection
            except BaseException:
                events.append("rollback")
                raise
            finally:
                events.append("db-exit")

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
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                return_value=attempt.calibration_payload()["review_artifact_fingerprint"],
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
                "mediaforce.web.runtime.av1_validation_derivation.append_content_intent_boundary_observation",
                side_effect=lambda *_args: (
                    events.append("append")
                    or (_ for _ in ()).throw(
                        ContentIntentObservationConflictError("conflict")
                    )
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.ensure_av1_validation_derivation_terminal_intent",
                side_effect=lambda *_args, **_kwargs: events.append(
                    "safety-terminal-intent"
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.ensure_av1_validation_derivation_terminal_record",
                side_effect=lambda *_args, **_kwargs: events.append(
                    "safety-terminal-record"
                ),
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
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        self.assertEqual(events, [
            "db-enter",
            "begin",
            "append",
            "rollback",
            "db-exit",
            "safety-terminal-intent",
            "safety-terminal-record",
        ])

    def test_derivation_verdict_terminalizes_source_exit_failure_after_intent(
            self,
    ) -> None:
        assignment = self.plan.assignments[0]
        attempt = self._review_pending_attempt()
        observation = _observation(
            assignment=assignment,
            source_identity=_source_identity(self.partition, assignment),
            crf=28.0,
            bitrate=1_000_000,
            verdict="acceptable",
        )
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        events: list[str] = []

        @contextmanager
        def database(
                _path: Path,
                **_kwargs: object,
        ) -> Iterator[SimpleNamespace]:
            events.append("db-enter")
            connection = SimpleNamespace(
                exec_driver_sql=lambda _sql: events.append("begin")
            )
            try:
                yield connection
            except BaseException:
                events.append("rollback")
                raise
            finally:
                events.append("db-exit")

        def fail_final_quiet() -> None:
            events.append("source-final-quiet")
            raise AV1ValidationPartitionError("source changed on session exit")

        @contextmanager
        def source_sha256_resolver_context(
                *_args: object,
                **_kwargs: object,
        ) -> Iterator[object]:
            session = _SourceSHA256Session(
                on_verify=lambda: events.append("source-verify"),
                on_quiet=fail_final_quiet,
            )
            try:
                yield session
            finally:
                session.assert_quiet()

        def freeze_verdict_intent(
                *args: object,
                **kwargs: object,
        ) -> dict[str, object]:
            events.append("verdict-intent")
            return resolve_av1_validation_derivation_verdict_intent(
                *args,
                **kwargs,
            )

        def persist_safety_terminal_intent(
                directory: Path,
                terminal: AV1ValidationDerivationTerminalRecord,
                **kwargs: object,
        ) -> Path:
            events.append("safety-terminal-intent")
            return ensure_av1_validation_derivation_terminal_intent(
                directory,
                terminal,
                **kwargs,
            )

        def persist_safety_terminal_record(
                directory: Path,
                terminal: AV1ValidationDerivationTerminalRecord,
                **kwargs: object,
        ) -> Path:
            events.append("safety-terminal-record")
            return ensure_av1_validation_derivation_terminal_record(
                directory,
                terminal,
                **kwargs,
            )

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
                "mediaforce.web.runtime.av1_validation_derivation.av1_validation_partition_source_sha256_resolver",
                side_effect=source_sha256_resolver_context,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                return_value=attempt.calibration_payload()["review_artifact_fingerprint"],
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.resolve_av1_validation_derivation_verdict_intent",
                side_effect=freeze_verdict_intent,
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
                "mediaforce.web.runtime.av1_validation_derivation.append_content_intent_boundary_observation",
                side_effect=lambda *_args: events.append("append"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.ensure_av1_validation_derivation_terminal_intent",
                side_effect=persist_safety_terminal_intent,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.ensure_av1_validation_derivation_terminal_record",
                side_effect=persist_safety_terminal_record,
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
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        self.assertEqual(events, [
            "db-enter",
            "begin",
            "verdict-intent",
            "append",
            "source-verify",
            "source-final-quiet",
            "rollback",
            "db-exit",
            "safety-terminal-intent",
            "safety-terminal-record",
        ])
        self.assertEqual(
            load_av1_validation_derivation_terminal_records(
                self.runtime_artifact_root / "terminal-records"
            ),
            (terminal,),
        )

    def test_derivation_verdict_terminal_write_failure_is_retryable(self) -> None:
        assignment = self.plan.assignments[0]
        attempt = self._review_pending_attempt()
        observation = _observation(
            assignment=assignment,
            source_identity=_source_identity(self.partition, assignment),
            crf=28.0,
            bitrate=1_000_000,
            verdict="acceptable",
        )
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        events: list[str] = []

        @contextmanager
        def database(
                _path: Path,
                **_kwargs: object,
        ) -> Iterator[SimpleNamespace]:
            events.append("db-enter")
            connection = SimpleNamespace(
                exec_driver_sql=lambda _sql: events.append("begin")
            )
            try:
                yield connection
            except BaseException:
                events.append("rollback")
                raise
            finally:
                events.append("db-exit")

        def fail_terminal_intent(*_args: object, **_kwargs: object) -> None:
            events.append("observed-terminal-intent")
            raise AV1ValidationDerivationError("terminal write failed")

        @contextmanager
        def source_sha256_resolver_context(
                *_args: object,
                **_kwargs: object,
        ) -> Iterator[object]:
            session = _SourceSHA256Session(
                on_verify=lambda: events.append("source-verify"),
                on_quiet=lambda: events.append("source-final-quiet"),
            )
            try:
                yield session
            finally:
                session.assert_quiet()

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
                "mediaforce.web.runtime.av1_validation_derivation.av1_validation_partition_source_sha256_resolver",
                side_effect=source_sha256_resolver_context,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                return_value=attempt.calibration_payload()["review_artifact_fingerprint"],
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
                "mediaforce.web.runtime.av1_validation_derivation.append_content_intent_boundary_observation",
                side_effect=lambda *_args: events.append("append"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.ensure_av1_validation_derivation_terminal_intent",
                side_effect=fail_terminal_intent,
            ) as write_terminal_intent,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.ensure_av1_validation_derivation_terminal_record"
            ) as write_terminal_record,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "terminal write failed",
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
        self.assertEqual(write_terminal_intent.call_count, 1)
        self.assertEqual(write_terminal_intent.call_args.args[1].status, "observed")
        write_terminal_record.assert_not_called()
        self.assertEqual(events, [
            "db-enter",
            "begin",
            "append",
            "source-verify",
            "source-final-quiet",
            "observed-terminal-intent",
            "rollback",
            "db-exit",
        ])

    def test_derivation_verdict_retries_after_post_publication_commit_failure(self) -> None:
        assignment = self.plan.assignments[0]
        attempt = self._review_pending_attempt()
        observation = _observation(
            assignment=assignment,
            source_identity=_source_identity(self.partition, assignment),
            crf=28.0,
            bitrate=1_000_000,
            verdict="acceptable",
        )
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        database_calls = 0

        @contextmanager
        def database(
                _path: Path,
                **_kwargs: object,
        ) -> Iterator[SimpleNamespace]:
            nonlocal database_calls
            database_calls += 1
            yield SimpleNamespace(exec_driver_sql=lambda _sql: None)
            if database_calls == 1:
                raise SQLAlchemyError("commit failed")

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
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                return_value=attempt.calibration_payload()["review_artifact_fingerprint"],
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
                "mediaforce.web.runtime.av1_validation_derivation.append_content_intent_boundary_observation"
            ) as append_observation,
        ):
            with self.assertRaisesRegex(SQLAlchemyError, "commit failed"):
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
        self.assertEqual(database_calls, 2)
        self.assertEqual(append_observation.call_count, 2)
        self.assertEqual(terminal.status, "observed")
        self.assertEqual(
            load_av1_validation_derivation_terminal_records(
                self.runtime_artifact_root / "terminal-records"
            ),
            (terminal,),
        )

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

    def test_derivation_verdict_terminalizes_observation_conflict_as_safety_stop(self) -> None:
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
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        write_terminal.assert_called_once()
        self.assertEqual(write_terminal.call_args.args[1], terminal)

    def test_derivation_verdict_terminalizes_changed_review_media(self) -> None:
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
        ):
            terminal = record_av1_validation_derivation_visual_verdict(
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
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
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
        def database(
                _path: Path,
                **_kwargs: object,
        ) -> Iterator[SimpleNamespace]:
            self.assertTrue(lock_held)
            events.append("db-enter")
            try:
                yield SimpleNamespace(exec_driver_sql=lambda _sql: None)
            finally:
                events.append("db-exit")

        def record_event(label: str) -> None:
            self.assertTrue(lock_held)
            events.append(label)

        def validate_current_inputs(*_args: object, **_kwargs: object) -> None:
            record_event("partition-current")

        def validate_source_commitments(
                _plan: object,
                *,
                resolver: object,
        ) -> None:
            self.assertIsInstance(resolver, _SourceSHA256Session)
            record_event("source-commitments")

        def record_terminal_artifact(
                label: str,
                kwargs: dict[str, object],
        ) -> None:
            before_publish = kwargs.get("before_publish")
            self.assertTrue(callable(before_publish))
            assert callable(before_publish)
            before_publish()
            record_event(label)

        @contextmanager
        def source_sha256_resolver_context(
                *_args: object,
                **_kwargs: object,
        ) -> Iterator[object]:
            session = _SourceSHA256Session(
                on_verify=lambda: record_event("source-verify"),
                on_quiet=lambda: record_event("source-quiet"),
            )
            yield session
            session.assert_quiet()

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
                side_effect=validate_current_inputs,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_source_commitments",
                side_effect=validate_source_commitments,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.av1_validation_partition_source_sha256_resolver",
                side_effect=source_sha256_resolver_context,
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
                side_effect=lambda *_args, **kwargs: record_terminal_artifact(
                    "terminal-intent",
                    kwargs,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.append_content_intent_boundary_observation",
                side_effect=lambda *_args, **_kwargs: record_event("db-append"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.ensure_av1_validation_derivation_terminal_record",
                side_effect=lambda *_args, **kwargs: record_terminal_artifact(
                    "terminal-record",
                    kwargs,
                ),
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
            "source-commitments",
            "execution-contract",
            "media-recheck",
            "execution-contract",
            "verdict-intent",
            "db-append",
            "execution-contract",
            "source-verify",
            "source-quiet",
            "terminal-intent",
            "terminal-record",
            "db-exit",
            "lock-exit",
        ])

    def test_derivation_verdict_terminalizes_current_input_drift(self) -> None:
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
                side_effect=AV1ValidationPartitionError("current inputs drifted"),
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
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        review_fingerprint.assert_not_called()
        verdict_intent.assert_not_called()
        append_observation.assert_not_called()
        write_terminal.assert_called_once()
        self.assertEqual(write_terminal.call_args.args[1], terminal)

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
            self.plan.statistics_contract_sha256,
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

    def test_proposal_retry_reuses_timestamp_after_parent_fsync_failure(self) -> None:
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
        for attempt in self._attempts(records):
            write_av1_validation_derivation_attempt(
                self.runtime_artifact_root / "attempts",
                attempt,
            )
        for record in records:
            write_av1_validation_derivation_terminal_record(
                self.runtime_artifact_root / "terminal-records",
                record,
            )
        current_observations = self._current_observations(records)
        args = SimpleNamespace(
            action="build-derivation-proposal",
            manifest=V2_MANIFEST_PATH,
            partition=self.runtime_artifact_root / "partition.json",
            plan=self.runtime_artifact_root / "plan.json",
            key=self.runtime_artifact_root / "partition.key",
            config=Path("unused.toml"),
            cell_plan_id=cell_plan_id,
            json_output=True,
        )
        proposal_path = (
            self.runtime_artifact_root / "proposals" / f"{cell_plan_id}.json"
        )
        proposal_published = False
        real_fsync = os.fsync
        real_rename = _rename_owner_only_exclusive

        def fail_proposal_publish_fsync(descriptor: int) -> None:
            if proposal_published and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(errno.EIO, "directory fsync failed")
            real_fsync(descriptor)

        def track_proposal_publish(
                *,
                parent_descriptor: int,
                source_name: str,
                destination_name: str,
        ) -> None:
            nonlocal proposal_published
            real_rename(
                parent_descriptor=parent_descriptor,
                source_name=source_name,
                destination_name=destination_name,
            )
            if destination_name == f"{cell_plan_id}.json":
                proposal_published = True

        recovered_parent_sync = False

        def track_recovery_fsync(descriptor: int) -> None:
            nonlocal recovered_parent_sync
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                recovered_parent_sync = True
            real_fsync(descriptor)

        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_canonical_derivation_plan",
                return_value=(self.plan, self.runtime_artifact_root),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_derivation_partition_for_evaluation",
                side_effect=lambda **_kwargs: _context_value((
                    self.partition,
                    _SourceSHA256Session(),
                )),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_current_av1_validation_derivation_observations",
                return_value=current_observations,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "assert_av1_validation_derivation_execution_environment",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_now_iso",
                return_value="2026-07-28T03:00:00Z",
            ) as now_iso,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_print_partition_payload",
            ),
        ):
            with (
                patch(
                    "mediaforce.tuning.av1_validation_derivation._rename_owner_only_exclusive",
                    side_effect=track_proposal_publish,
                ),
                patch(
                    "mediaforce.tuning.av1_validation_derivation.os.fsync",
                    side_effect=fail_proposal_publish_fsync,
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "could not be written safely",
                ),
            ):
                verify_av1_cold_start_preregistration._run_derivation_proposal_action(
                    args,
                    config=self.runtime_config,
                )
            self.assertTrue(proposal_path.exists())
            with patch(
                "mediaforce.tuning.av1_validation_derivation.os.fsync",
                side_effect=track_recovery_fsync,
            ):
                exit_code = (
                    verify_av1_cold_start_preregistration._run_derivation_proposal_action(
                        args,
                        config=self.runtime_config,
                    )
                )
        self.assertEqual(exit_code, 0)
        self.assertTrue(recovered_parent_sync)
        self.assertEqual(now_iso.call_args_list, [call(), call()])
        persisted = load_av1_validation_derivation_candidate_proposal(
            self.runtime_artifact_root,
            plan=self.plan,
            cell_plan_id=cell_plan_id,
        )
        self.assertEqual(persisted.proposed_at, "2026-07-28T03:00:00Z")

        conflicting = self._candidate_proposal(
            proposed_at="2026-07-28T03:00:01Z",
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "conflicts with an immutable existing proposal",
        ):
            write_av1_validation_derivation_candidate_proposal(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=conflicting,
            )

    def test_proposal_expiry_crossing_is_checked_at_publication(self) -> None:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        proposal = self._candidate_proposal(
            proposed_at="2026-07-31T23:59:59Z",
        )
        evaluation = SimpleNamespace(proposal=proposal)
        args = SimpleNamespace(
            action="build-derivation-proposal",
            manifest=V2_MANIFEST_PATH,
            partition=self.runtime_artifact_root / "partition.json",
            plan=self.runtime_artifact_root / "plan.json",
            key=self.runtime_artifact_root / "partition.key",
            config=Path("unused.toml"),
            cell_plan_id=cell_plan_id,
            json_output=True,
        )

        def publish_proposal(*_args: object, **kwargs: object) -> None:
            before_publish = kwargs.get("before_publish")
            self.assertTrue(callable(before_publish))
            assert callable(before_publish)
            before_publish()
            self.fail("expired proposal reached publication")

        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_canonical_derivation_plan",
                return_value=(self.plan, self.runtime_artifact_root),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_derivation_partition_for_evaluation",
                side_effect=lambda **_kwargs: _context_value((
                    self.partition,
                    _SourceSHA256Session(),
                )),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_derivation_attempts",
                return_value=(),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_derivation_terminal_records",
                return_value=(),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_current_av1_validation_derivation_observations",
                return_value={},
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "evaluate_av1_validation_derivation_candidate",
                return_value=evaluation,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "av1_validation_derivation_candidate_evaluation_public_summary",
                return_value={},
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "assert_av1_validation_derivation_execution_environment",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_now_iso",
                side_effect=("2026-07-31T23:59:59Z", VALID_UNTIL),
            ) as now_iso,
            patch.object(
                verify_av1_cold_start_preregistration,
                "write_av1_validation_derivation_candidate_proposal",
                side_effect=publish_proposal,
            ) as write_proposal,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_print_partition_payload",
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "outside its authorization window",
            ),
        ):
            verify_av1_cold_start_preregistration._run_derivation_proposal_action(
                args,
                config=self.runtime_config,
            )
        self.assertEqual(now_iso.call_args_list, [call(), call()])
        write_proposal.assert_called_once()
        self.assertFalse(
            (
                self.runtime_artifact_root
                / "proposals"
                / f"{cell_plan_id}.json"
            ).exists()
        )

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

    def test_candidate_lock_samples_timestamp_after_live_source_and_evidence_preflight(
            self,
    ) -> None:
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
        attempts = self._attempts(records)
        current_observations = self._current_observations(records)
        proposal = self._candidate_proposal()
        locked_at = "2026-07-28T03:06:00Z"
        current_evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=self.manifest,
            plan=self.plan,
            partition=self.partition,
            cell_plan_id=cell_plan_id,
            attempts=attempts,
            records=records,
            current_observations=current_observations,
            proposed_at=locked_at,
        )
        events: list[str] = []
        expected_envelope = SimpleNamespace(payload_sha256="sha256:test")

        @contextmanager
        def database(
                _path: Path,
                **_kwargs: object,
        ) -> Iterator[SimpleNamespace]:
            yield SimpleNamespace(exec_driver_sql=lambda _sql: None)

        @contextmanager
        def source_sha256_resolver_context(
                *_args: object,
                **_kwargs: object,
        ) -> Iterator[object]:
            session = _SourceSHA256Session(
                on_verify=lambda: events.append("source-verify"),
                on_quiet=lambda: events.append("source-final-quiet"),
            )
            try:
                yield session
            finally:
                session.assert_quiet()

        def load_current_evidence(**_kwargs: object) -> dict[
            str,
            ContentIntentBoundaryObservation,
        ]:
            events.append("current-evidence")
            return current_observations

        def sample_clock() -> str:
            events.append("clock")
            return locked_at

        def evaluate_current_candidate(**kwargs: object) -> object:
            events.append("evaluate")
            self.assertEqual(kwargs["proposed_at"], locked_at)
            return current_evaluation

        def publish_candidate_lock(*_args: object, **kwargs: object) -> object:
            before_publish = kwargs.get("before_publish")
            self.assertTrue(callable(before_publish))
            assert callable(before_publish)
            before_publish()
            events.append("publish")
            self.assertEqual(kwargs["locked_at"], locked_at)
            return expected_envelope

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_config",
                return_value=self.runtime_config,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._load_canonical_av1_validation_derivation_plan",
                return_value=(self.plan, self.runtime_artifact_root),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.exclusive_mediaforce_runtime_lock",
                return_value=nullcontext(),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_candidate_proposal",
                return_value=proposal,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_attempts",
                return_value=attempts,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_terminal_records",
                return_value=records,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_review_claims",
                return_value=(),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_review_envelopes",
                return_value=(),
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
                side_effect=lambda *_args, **_kwargs: events.append(
                    "current-inputs"
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_source_commitments",
                side_effect=lambda *_args, **_kwargs: events.append(
                    "source-commitments"
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.av1_validation_partition_source_sha256_resolver",
                side_effect=source_sha256_resolver_context,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._load_current_derivation_observations_from_connection",
                side_effect=load_current_evidence,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.evaluate_av1_validation_derivation_candidate",
                side_effect=evaluate_current_candidate,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._finalize_and_write_av1_validation_derivation_candidate_lock",
                side_effect=publish_candidate_lock,
            ),
        ):
            envelope = finalize_runtime_av1_validation_derivation_candidate_lock(
                config_path=Path("unused.toml"),
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan_path=self.runtime_artifact_root / "plan.json",
                cell_plan_id=cell_plan_id,
                repository_commit=self.plan.repository_commit,
                repository_tree=self.plan.repository_tree,
                now_iso=sample_clock,
            )
        self.assertIs(envelope, expected_envelope)
        self.assertEqual(events, [
            "current-inputs",
            "source-commitments",
            "current-evidence",
            "source-verify",
            "clock",
            "evaluate",
            "source-final-quiet",
            "clock",
            "publish",
            "source-final-quiet",
        ])
        self.assertEqual(events.count("clock"), 2)

    def test_candidate_lock_expiry_crossing_is_checked_after_source_verification(
            self,
    ) -> None:
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
        attempts = self._attempts(records)
        current_observations = self._current_observations(records)
        proposal = self._candidate_proposal()
        events: list[str] = []

        @contextmanager
        def database(
                _path: Path,
                **_kwargs: object,
        ) -> Iterator[SimpleNamespace]:
            yield SimpleNamespace(exec_driver_sql=lambda _sql: None)

        @contextmanager
        def source_sha256_resolver_context(
                *_args: object,
                **_kwargs: object,
        ) -> Iterator[object]:
            session = _SourceSHA256Session(
                on_verify=lambda: events.append("source-verify"),
                on_quiet=lambda: events.append("source-final-quiet"),
            )
            try:
                yield session
            finally:
                session.assert_quiet()

        def load_current_evidence(**_kwargs: object) -> dict[
            str,
            ContentIntentBoundaryObservation,
        ]:
            events.append("current-evidence")
            return current_observations

        locked_at = "2026-07-28T03:06:00Z"
        clock_values = iter((locked_at, VALID_UNTIL))

        def sample_expired_clock() -> str:
            events.append("clock")
            return next(clock_values)

        def publish_candidate_lock(*_args: object, **kwargs: object) -> object:
            before_publish = kwargs.get("before_publish")
            self.assertTrue(callable(before_publish))
            assert callable(before_publish)
            before_publish()
            self.fail("expired candidate lock reached publication")

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_config",
                return_value=self.runtime_config,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._load_canonical_av1_validation_derivation_plan",
                return_value=(self.plan, self.runtime_artifact_root),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.exclusive_mediaforce_runtime_lock",
                return_value=nullcontext(),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_candidate_proposal",
                return_value=proposal,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_attempts",
                return_value=attempts,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_terminal_records",
                return_value=records,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_review_claims",
                return_value=(),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.load_av1_validation_derivation_review_envelopes",
                return_value=(),
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
                side_effect=lambda *_args, **_kwargs: events.append(
                    "current-inputs"
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_source_commitments",
                side_effect=lambda *_args, **_kwargs: events.append(
                    "source-commitments"
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.av1_validation_partition_source_sha256_resolver",
                side_effect=source_sha256_resolver_context,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._load_current_derivation_observations_from_connection",
                side_effect=load_current_evidence,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._finalize_and_write_av1_validation_derivation_candidate_lock",
                side_effect=publish_candidate_lock,
            ) as publish_candidate_lock_mock,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "outside its authorization window",
            ),
        ):
            finalize_runtime_av1_validation_derivation_candidate_lock(
                config_path=Path("unused.toml"),
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan_path=self.runtime_artifact_root / "plan.json",
                cell_plan_id=cell_plan_id,
                repository_commit=self.plan.repository_commit,
                repository_tree=self.plan.repository_tree,
                now_iso=sample_expired_clock,
            )
        publish_candidate_lock_mock.assert_called_once()
        self.assertEqual(events, [
            "current-inputs",
            "source-commitments",
            "current-evidence",
            "source-verify",
            "clock",
            "source-final-quiet",
            "clock",
            "source-final-quiet",
        ])
        self.assertFalse(
            (
                self.runtime_artifact_root
                / "candidate-locks"
                / f"{cell_plan_id}.json"
            ).exists()
        )

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
                repository_commit=REVIEW_REPOSITORY_COMMIT,
                repository_tree=REVIEW_REPOSITORY_TREE,
                lane=lane,
                review_run_id=f"00000000-0000-0000-0000-{index:012x}",
                review_runner_canonical_path_sha256=(
                    self.plan.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.plan.review_runner_binary_sha256
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
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "repository snapshot drifted",
        ):
            build_av1_validation_derivation_review_claim(
                plan=self.plan,
                proposal=evaluation.proposal,
                repository_commit="3" * 40,
                repository_tree="4" * 40,
                lane=review_claims[-1].lane,
                review_run_id=review_claims[-1].review_run_id,
                review_runner_canonical_path_sha256=(
                    self.plan.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.plan.review_runner_binary_sha256
                ),
                claimed_at=review_claims[-1].claimed_at,
            )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "repository snapshot drifted",
        ):
            finalize_av1_validation_derivation_candidate_lock(
                plan=self.plan,
                proposal=evaluation.proposal,
                review_claims=review_claims,
                reviews=reviews,
                current_evaluation=current_evaluation,
                locked_at=locked_at,
                repository_commit="3" * 40,
                repository_tree="4" * 40,
            )
        with self.assertRaisesRegex(AV1ValidationDerivationError, "all five"):
            finalize_av1_validation_derivation_candidate_lock(
                plan=self.plan,
                proposal=evaluation.proposal,
                review_claims=review_claims,
                reviews=reviews[:-1],
                current_evaluation=current_evaluation,
                locked_at=locked_at,
                repository_commit=self.plan.repository_commit,
                repository_tree=self.plan.repository_tree,
            )
        duplicate_evidence_claims = [
            build_av1_validation_derivation_review_claim(
                plan=self.plan,
                proposal=evaluation.proposal,
                repository_commit=REVIEW_REPOSITORY_COMMIT,
                repository_tree=REVIEW_REPOSITORY_TREE,
                lane=lane,
                review_run_id=f"10000000-0000-0000-0000-{index:012x}",
                review_runner_canonical_path_sha256=(
                    self.plan.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.plan.review_runner_binary_sha256
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
                plan=self.plan,
                proposal=evaluation.proposal,
                review_claims=duplicate_evidence_claims,
                reviews=duplicate_evidence_reviews,
                current_evaluation=current_evaluation,
                locked_at=locked_at,
                repository_commit=self.plan.repository_commit,
                repository_tree=self.plan.repository_tree,
            )
        lock = finalize_av1_validation_derivation_candidate_lock(
            plan=self.plan,
            proposal=evaluation.proposal,
            review_claims=review_claims,
            reviews=reviews,
            current_evaluation=current_evaluation,
            locked_at=locked_at,
            repository_commit=self.plan.repository_commit,
            repository_tree=self.plan.repository_tree,
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
            with (
                patch(
                    "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
                    return_value=2 ** 63 - 1,
                ),
                self.assertRaises(
                    AV1ValidationDerivationPublicationDeadlineError
                ),
            ):
                load_av1_validation_derivation_review_envelopes(
                    artifact_root,
                    plan=self.plan,
                    proposal=evaluation.proposal,
                    claims=loaded_claims,
                )
            candidate_lock_directory = artifact_root / "candidate-locks"
            _bind_owner_only_directory(
                candidate_lock_directory,
                kind="candidate_locks",
                binding_id=self.plan.plan_id,
                binding_digest=self.plan.authorization.authorization_id,
            )
            lock_path = (
                candidate_lock_directory / f"{cell_plan_id}.json"
            ).resolve()
            candidate_published = False
            real_fsync = os.fsync
            real_rename = _rename_owner_only_exclusive

            def fail_candidate_publish_fsync(descriptor: int) -> None:
                if (
                    candidate_published
                    and stat.S_ISDIR(os.fstat(descriptor).st_mode)
                ):
                    raise OSError(errno.EIO, "directory fsync failed")
                real_fsync(descriptor)

            def track_candidate_publish(
                    *,
                    parent_descriptor: int,
                    source_name: str,
                    destination_name: str,
            ) -> None:
                nonlocal candidate_published
                real_rename(
                    parent_descriptor=parent_descriptor,
                    source_name=source_name,
                    destination_name=destination_name,
                )
                if destination_name == f"{cell_plan_id}.json":
                    candidate_published = True

            with (
                patch(
                    "mediaforce.tuning.av1_validation_derivation._rename_owner_only_exclusive",
                    side_effect=track_candidate_publish,
                ),
                patch(
                    "mediaforce.tuning.av1_validation_derivation.os.fsync",
                    side_effect=fail_candidate_publish_fsync,
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "could not be written safely",
                ),
            ):
                finalize_and_write_av1_validation_derivation_candidate_lock(
                    artifact_root,
                    plan=self.plan,
                    proposal=evaluation.proposal,
                    review_claims=loaded_claims,
                    review_envelopes=loaded_envelopes,
                    current_evaluation=current_evaluation,
                    locked_at=locked_at,
                    repository_commit=self.plan.repository_commit,
                    repository_tree=self.plan.repository_tree,
                )
            self.assertTrue(lock_path.exists())
            recovered_parent_sync = False

            def track_recovery_fsync(descriptor: int) -> None:
                nonlocal recovered_parent_sync
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    recovered_parent_sync = True
                real_fsync(descriptor)

            with patch(
                "mediaforce.tuning.av1_validation_derivation.os.fsync",
                side_effect=track_recovery_fsync,
            ):
                persisted_envelope = finalize_and_write_av1_validation_derivation_candidate_lock(
                    artifact_root,
                    plan=self.plan,
                    proposal=evaluation.proposal,
                    review_claims=loaded_claims,
                    review_envelopes=loaded_envelopes,
                    current_evaluation=current_evaluation,
                    locked_at=locked_at,
                    repository_commit=self.plan.repository_commit,
                    repository_tree=self.plan.repository_tree,
                )
            self.assertTrue(recovered_parent_sync)
            self.assertEqual(persisted_envelope.candidate_lock, lock)
            self.assertEqual(
                _av1_validation_derivation_candidate_locked_at(
                    artifact_root=artifact_root,
                    plan=self.plan,
                    cell_plan_id=cell_plan_id,
                    clock=lambda: self.fail(
                        "existing candidate-lock recovery sampled a new clock"
                    ),
                ),
                locked_at,
            )
            self.assertEqual(
                load_verified_av1_validation_derivation_candidate_lock(
                    artifact_root,
                    plan=self.plan,
                    proposal=evaluation.proposal,
                    review_claims=loaded_claims,
                    review_envelopes=loaded_envelopes,
                    current_evaluation=current_evaluation,
                    cell_plan_id=cell_plan_id,
                    repository_commit=self.plan.repository_commit,
                    repository_tree=self.plan.repository_tree,
                ),
                persisted_envelope,
            )
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
                plan=self.plan,
                proposal=evaluation.proposal,
                review_claims=review_claims,
                reviews=reviews,
                current_evaluation=stale_evaluation,
                locked_at=locked_at,
                repository_commit=self.plan.repository_commit,
                repository_tree=self.plan.repository_tree,
            )

    def test_candidate_lock_uses_merged_v1_token_shape(self) -> None:
        source_tokens = tuple(f"source_token_{index:02d}" for index in range(6))
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
            "derivation_source_count": 6,
            "derivation_source_tokens": source_tokens,
            "derivation_series_tokens": series_tokens,
            "derivation_source_group_tokens": shared_tokens,
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
        self.assertEqual(candidate_lock.derivation_source_count, 6)
        self.assertEqual(len(candidate_lock.derivation_source_group_tokens), 6)
        self.assertNotIn("derivation_title_tokens", candidate_lock.to_payload())
        self.assertNotIn(
            "derivation_source_group_observation_tokens",
            candidate_lock.to_payload(),
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
            runtime_config.paths.db_path.touch(mode=0o600)
            stdout = io.StringIO()
            retry_stdout = io.StringIO()
            current_inputs = (
                self.manifest,
                self.partition,
                self.token_key,
                _SourceSHA256Session(),
                self.source_commitments,
            )
            argv = [
                "create-derivation-plan",
                str(V2_MANIFEST_PATH),
                str(root / "eligibility.json"),
                str(root / "partition.json"),
                "--key",
                str(root / "partition.key"),
                "--valid-until",
                VALID_UNTIL,
                "--json",
            ]
            with (
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_load_current_derivation_inputs",
                    side_effect=lambda *_args, **_kwargs: _context_value(
                        current_inputs
                    ),
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
                ) as now_iso,
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_review_runner_identity",
                    return_value=(
                        root / "private-code-runner",
                        self.plan.review_runner_canonical_path_sha256,
                        self.plan.review_runner_binary_sha256,
                        REVIEW_RUNNER_BYTES,
                    ),
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_repository_review_identity",
                    return_value=(
                        self.plan.repository_commit,
                        self.plan.repository_tree,
                    ),
                ),
            ):
                with redirect_stdout(stdout):
                    exit_code = verify_av1_cold_start_preregistration.main(argv)
                with redirect_stdout(retry_stdout):
                    retry_exit_code = verify_av1_cold_start_preregistration.main(argv)
            self.assertEqual(exit_code, 0)
            self.assertEqual(retry_exit_code, 0)
            now_iso.assert_called_once_with()
            payload = json.loads(stdout.getvalue())
            self.assertEqual(json.loads(retry_stdout.getvalue()), payload)
            self.assertEqual(payload["derivation_assignment_count"], 24)
            plan_path = (
                runtime_config.paths.web_state_dir
                / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
                / self.partition.partition_id
                / "plan.json"
            )
            self.assertTrue(plan_path.is_file())
            self.assertEqual(
                load_av1_validation_derivation_plan(plan_path).authorization.authorized_at,
                AUTHORIZED_AT,
            )
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


class AV1ValidationDerivationLockOwnershipTests(unittest.TestCase):
    def test_forged_runtime_lease_cannot_authorize_writes(self) -> None:
        forged_lease = MediaforceRuntimeLease(
            namespace_keys=(),
            owner_pid=os.getpid(),
        )

        with self.assertRaises(MediaforceRuntimeLockOwnershipError):
            with forged_lease.bind():
                pass

    def test_derivation_writers_reject_calls_without_runtime_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_path = root / "private" / "artifact.json"
            binding_path = root / "private" / "attempts"

            with self.assertRaises(MediaforceRuntimeLockOwnershipError):
                _write_owner_only(artifact_path, b"{}")
            with self.assertRaises(MediaforceRuntimeLockOwnershipError):
                _bind_owner_only_directory(
                    binding_path,
                    kind="attempts",
                    binding_id="av1vdplan1_test",
                    binding_digest="av1vdauth2_test",
                )

            self.assertFalse(artifact_path.parent.exists())

    def test_derivation_writers_accept_only_active_runtime_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                paths=SimpleNamespace(
                    config_path=root / "config.toml",
                    db_path=root / "mediaforce.sqlite3",
                    web_state_dir=root / "state",
                )
            )
            artifact_path = root / "private" / "artifact.json"
            binding_path = root / "private" / "attempts"
            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "derivation-writer-test"},
            ) as lease:
                _write_owner_only(artifact_path, b"{}")
                _bind_owner_only_directory(
                    binding_path,
                    kind="attempts",
                    binding_id="av1vdplan1_test",
                    binding_digest="av1vdauth2_test",
                )

            self.assertTrue(artifact_path.is_file())
            self.assertTrue((binding_path / ".binding").is_file())
            with self.assertRaises(MediaforceRuntimeLockOwnershipError):
                with lease.bind():
                    pass


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
        artifact_fingerprint=f"cira3_{assignment.assignment_id}",
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
    artifact = f"cira3_{assignment.assignment_id}"
    duration_seconds = 3_600.0
    predicted_video_bytes = round((bitrate * duration_seconds) / 8)
    source_size_bytes = SOURCE_SIZE_BYTES
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
            "source_size_bytes": source_size_bytes,
            "source_snapshot_sha256": _source_sha256_for_identity(source_identity),
            "source_snapshot_size_bytes": source_size_bytes,
            "source_snapshot_content_version_fingerprint": source_identity,
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
        proposal: AV1ValidationDerivationCandidateProposal,
        claim: AV1ValidationDerivationReviewClaim,
        decision: Literal["approved", "rejected"] = "approved",
) -> bytes:
    proposal_id = str(getattr(proposal, "proposal_id"))
    proposal_payload_sha256 = str(getattr(proposal, "payload_sha256"))
    lane = str(getattr(claim, "lane"))
    review_run_id = str(getattr(claim, "review_run_id"))
    review_claim_id = str(getattr(claim, "claim_id"))
    review_claim_payload_sha256 = str(getattr(claim, "payload_sha256"))
    repository_commit = str(getattr(claim, "repository_commit"))
    repository_tree = str(getattr(claim, "repository_tree"))
    prompt = build_av1_validation_derivation_review_prompt(
        proposal=proposal,
        claim=claim,
    )
    marker = {
        "decision": decision,
        "lane": lane,
        "proposal_id": proposal_id,
        "proposal_payload_sha256": proposal_payload_sha256,
        "repository_commit": repository_commit,
        "repository_tree": repository_tree,
        "review_claim_id": review_claim_id,
        "review_claim_payload_sha256": review_claim_payload_sha256,
        "review_run_id": review_run_id,
    }
    final_message = (
        "Review complete.\n"
        "MEDIAFORCE_AV1_REVIEW_V2 "
        f"{canonical_json_bytes(marker).decode('utf-8')}"
    )
    stdout = "\n".join((
        canonical_json_bytes({
            "provider": "test",
            "model": "test-model",
            "workdir": "/private/test",
            "approval": "never",
            "sandbox": "read-only",
        }).decode("utf-8"),
        canonical_json_bytes({"prompt": prompt}).decode("utf-8"),
        canonical_json_bytes({
            "msg": {
                "type": "agent_message",
                "message": final_message,
            }
        }).decode("utf-8"),
        canonical_json_bytes({
            "msg": {
                "type": "task_lifecycle",
                "phase": "quiescent",
                "last_agent_message": final_message,
            }
        }).decode("utf-8"),
    ))
    return canonical_json_bytes({
        "schema": "mediaforce.av1_derivation_agent_review_run",
        "schema_version": 1,
        "review_run_id": review_run_id,
        "reviewer_token": f"agent:{review_run_id}",
        "proposal_id": proposal_id,
        "proposal_payload_sha256": proposal_payload_sha256,
        "repository_commit": repository_commit,
        "repository_tree": repository_tree,
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
        "proposal": proposal.to_payload(),
        "review_claim": claim.to_payload(),
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
