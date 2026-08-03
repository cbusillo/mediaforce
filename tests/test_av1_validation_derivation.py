from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from dataclasses import replace
import errno
import fcntl
import hashlib
from importlib import import_module, resources
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
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
    AV1_VALIDATION_DERIVATION_RUNTIME_FAILURE_REASON_CODES,
    AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_ALLOWLIST,
    AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA,
    AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA_VERSION,
    AV1_VALIDATION_DERIVATION_REVIEW_LANES,
    AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BLOB_BYTES,
    AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BUNDLE_BYTES,
    AV1_VALIDATION_DERIVATION_REVIEW_RECOVERY_SCHEMA,
    AV1_VALIDATION_DERIVATION_CHECKPOINTED_REVIEW_RUN_SCHEMA,
    AV1_VALIDATION_DERIVATION_STRUCTURED_REVIEW_RUN_SCHEMA,
    AV1_VALIDATION_DERIVATION_PERSONALIZATION_EXCLUSION_REASON,
    AV1ValidationDerivationAttempt,
    AV1ValidationDerivationCandidateProposal,
    AV1ValidationDerivationError,
    AV1ValidationDerivationPlan,
    AV1ValidationDerivationPublicationDeadlineError,
    AV1ValidationDerivationReviewClaim,
    AV1ValidationDerivationReviewEnvelope,
    AV1ValidationDerivationSourceCommitment,
    AV1ValidationDerivationTerminalRecord,
    AV1ValidationDerivationVerdictRetryMismatchError,
    _attempt_semantic_payload,
    _bind_owner_only_directory,
    _derivation_id,
    _payload_sha256,
    _read_owner_only_bytes,
    _rename_owner_only_exclusive,
    _fsync_owner_only_artifact,
    _av1_validation_derivation_review_set_sha256,
    _terminal_semantic_payload,
    _write_owner_only,
    assert_av1_validation_derivation_authorization_active,
    assert_av1_validation_derivation_source_commitments,
    av1_validation_derivation_review_analysis_sha256,
    av1_validation_derivation_review_developer_text,
    av1_validation_derivation_plan_public_summary,
    av1_validation_derivation_plan_from_payload,
    av1_validation_derivation_plan_source_commitment,
    av1_validation_derivation_source_commitment_sha256,
    av1_validation_derivation_statistics_contract_sha256,
    build_av1_validation_derivation_attempt,
    build_av1_validation_derivation_plan,
    build_av1_validation_derivation_source_commitments,
    build_av1_validation_derivation_review_claim,
    build_av1_validation_derivation_review_attestation,
    build_av1_validation_derivation_review_envelope,
    build_av1_validation_derivation_review_recovery_evidence,
    build_av1_validation_derivation_review_request,
    build_av1_validation_derivation_review_response_schema,
    build_av1_validation_derivation_terminal_record,
    ensure_av1_validation_derivation_verdict_claim,
    ensure_av1_validation_derivation_terminal_intent,
    ensure_av1_validation_derivation_terminal_record,
    evaluate_av1_validation_derivation_candidate,
    _finalize_and_write_av1_validation_derivation_candidate_lock as finalize_and_write_av1_validation_derivation_candidate_lock,
    finalize_av1_validation_derivation_candidate_lock,
    load_av1_validation_derivation_candidate_proposal,
    load_av1_validation_derivation_assignment_claims,
    load_av1_validation_derivation_attempt_publication_state,
    _load_verified_av1_validation_derivation_candidate_lock as load_verified_av1_validation_derivation_candidate_lock,
    load_av1_validation_derivation_plan,
    load_av1_validation_derivation_attempts,
    load_av1_validation_derivation_review_claims,
    load_av1_validation_derivation_review_envelope,
    load_av1_validation_derivation_review_envelopes,
    load_av1_validation_derivation_review_response_checkpoint,
    load_av1_validation_derivation_terminal_intents,
    load_av1_validation_derivation_terminal_records,
    resolve_av1_validation_derivation_verdict_intent,
    retain_av1_validation_derivation_publication_directories,
    validate_av1_validation_derivation_review_bundle,
    validate_av1_validation_derivation_review_run_evidence,
    validate_av1_validation_derivation_review_response,
    validate_av1_validation_derivation_plan_binding,
    write_av1_validation_derivation_candidate_proposal,
    write_av1_validation_derivation_plan,
    write_av1_validation_derivation_review_claim,
    write_av1_validation_derivation_review_envelope,
    write_av1_validation_derivation_review_response_checkpoint,
    write_av1_validation_derivation_attempt,
    write_av1_validation_derivation_assignment_claim,
    write_av1_validation_derivation_terminal_record,
)
from mediaforce.web.runtime.av1_validation_derivation import (
    AV1_VALIDATION_DERIVATION_MINIMUM_FREE_BYTES,
    _AV1_VALIDATION_DERIVATION_IMPLEMENTATION_FIXED_FILES,
    _AV1ValidationDerivationAssignmentPreflight,
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
    assert_av1_validation_derivation_source_snapshot_capacity,
    av1_validation_derivation_execution_environment_sha256,
    av1_validation_derivation_runtime_context_sha256,
    build_av1_validation_derivation_calibration_deps,
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
VALID_UNTIL = "2027-01-01T00:00:00Z"
JUST_BEFORE_VALID_UNTIL = "2026-12-31T23:59:59Z"
AFTER_VALID_UNTIL = "2027-01-01T00:00:01Z"
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
            verify_av1_cold_start_preregistration._canonical_system_git_binary(
                sys.platform
            ),
            "-C",
            str(repository),
            "-c",
            "user.name=Mediaforce Test",
            "-c",
            "user.email=mediaforce-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "init.templateDir=",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
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


def _create_preregistration_fixture_virtual_environment(
        repository: Path,
) -> Path:
    virtual_environment = repository / ".venv"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "venv",
            "--without-pip",
            str(virtual_environment),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    site_packages = next(
        Path(entry).resolve()
        for entry in sys.path
        if Path(entry).name == "site-packages"
        and (Path(entry) / "sqlalchemy").is_dir()
    )
    fixture_site_packages = (
        virtual_environment
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    if fixture_site_packages.is_symlink():
        fixture_site_packages.unlink()
    else:
        shutil.rmtree(fixture_site_packages)
    fixture_site_packages.symlink_to(site_packages, target_is_directory=True)
    return virtual_environment / "bin" / "python"


def _initialize_preregistration_test_repository(repository: Path) -> None:
    (repository / "scripts").mkdir(parents=True)
    (repository / "mediaforce").mkdir()
    (repository / "scripts" / "runner.py").write_text(
        "VALUE = 'tracked'\n",
        encoding="utf-8",
    )
    (repository / "mediaforce" / "__init__.py").write_text(
        '"""fixture"""\n',
        encoding="utf-8",
    )
    (repository / "mediaforce" / "module.py").write_text(
        "VALUE = 'tracked'\n",
        encoding="utf-8",
    )
    for arguments in (
        ("init", "-q"),
        ("add", "mediaforce", "scripts"),
        ("commit", "-qm", "preregistration fixture"),
    ):
        _run_test_git(repository, *arguments)


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
                create=True,
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

    def _record_approved_derivation_verdict(
            self,
            *,
            database: Callable[..., object],
            clock: Callable[[], str],
    ) -> AV1ValidationDerivationTerminalRecord:
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
                return_value=True,
            ),
        ):
            return record_av1_validation_derivation_visual_verdict(
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
                repository_identity_resolver=self._matching_repository_identity,
                recorded_at="2026-07-28T01:06:00Z",
                now_iso=clock,
            )

    def _matching_repository_identity(self) -> tuple[str, str]:
        return self.plan.repository_commit, self.plan.repository_tree

    def _drifted_repository_identity(self) -> tuple[str, str]:
        return "b" * 40, "c" * 40

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

    def _run_assignment_with_runtime_and_cleanup_result(
            self,
            *,
            failure_dependency: str | None,
            cleanup_failure: bool,
            failure_after_dependency: str | None = None,
            cleanup_base_exception: bool = False,
            cleanup_crosses_authorization: bool = False,
            malformed_result: bool = False,
            activity_guard_failure: Literal["enter", "exit"] | None = None,
            activity_guard_events: list[str] | None = None,
            preflight_failure: bool = False,
            source_snapshot_failure: bool = False,
    ) -> tuple[
        AV1ValidationDerivationAttempt,
        AV1ValidationDerivationTerminalRecord,
    ]:
        assignment = self.plan.assignments[0]
        source = next(
            item
            for item in self.partition.inventory_sources
            if item.local_item_id == assignment.local_item_id
        )
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
        attempts_directory = self.runtime_artifact_root / "attempts"
        terminal_records_directory = self.runtime_artifact_root / "terminal-records"
        cleanup_call_count = 0
        calibration_payload = self._review_pending_attempt().calibration_payload()

        def raise_runtime_failure(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("private runtime failure detail")

        def succeed_runtime_dependency(*_args: object, **_kwargs: object) -> None:
            return None

        def run_calibration(**kwargs: object) -> object:
            if failure_dependency is not None:
                dependency = getattr(kwargs["deps"], failure_dependency)
                dependency()
                raise AssertionError("runtime dependency failure did not propagate")
            if failure_after_dependency is not None:
                kwargs["progress_callback"]("inspecting_source")
                dependency = getattr(kwargs["deps"], failure_after_dependency)
                dependency()
                raise RuntimeError("private interstitial runtime failure detail")
            if malformed_result:
                return (dict(calibration_payload),)
            return dict(calibration_payload), None

        def purge(*_args: object, **_kwargs: object) -> None:
            nonlocal cleanup_call_count
            cleanup_call_count += 1
            if preflight_failure and cleanup_call_count == 1:
                raise RuntimeError("private preflight failure detail")
            if cleanup_base_exception and cleanup_call_count == 2:
                raise KeyboardInterrupt
            if cleanup_failure and cleanup_call_count == 2:
                raise RuntimeError("private cleanup failure detail")

        def clock() -> str:
            if cleanup_crosses_authorization and cleanup_call_count >= 2:
                return self.plan.authorization.valid_until
            return "2026-07-30T01:00:00Z"

        deps = build_av1_validation_derivation_calibration_deps()
        if failure_dependency is not None:
            deps = replace(
                deps,
                **{failure_dependency: raise_runtime_failure},
            )
        if failure_after_dependency is not None:
            deps = replace(
                deps,
                **{failure_after_dependency: succeed_runtime_dependency},
            )
        controller = ManagedProcessController()

        class ActivityGuard:
            def __enter__(self) -> None:
                if activity_guard_events is not None:
                    activity_guard_events.append("enter")
                if activity_guard_failure == "enter":
                    raise RuntimeError("private activity guard enter detail")

            def __exit__(
                    self,
                    _exc_type: object,
                    _exc_value: object,
                    _traceback: object,
            ) -> None:
                if activity_guard_events is not None:
                    activity_guard_events.append("exit")
                if activity_guard_failure == "exit":
                    raise RuntimeError("private activity guard exit detail")

        activity_guard = (
            patch.object(
                controller,
                "activity_guard",
                return_value=ActivityGuard(),
            )
            if activity_guard_failure is not None or activity_guard_events is not None
            else nullcontext()
        )
        pinned_source_context = (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._pinned_derivation_source",
                side_effect=RuntimeError("private source snapshot failure detail"),
            )
            if source_snapshot_failure
            else patch(
                "mediaforce.web.runtime.av1_validation_derivation._pinned_derivation_source",
                return_value=nullcontext(pinned_source),
            )
        )
        preflight = _AV1ValidationDerivationAssignmentPreflight(
            assignment=assignment,
            artifact_root=self.runtime_artifact_root,
            clock=clock,
        )
        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_runtime_context",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.probe_macos_file_integrity",
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.shutil.disk_usage",
                return_value=SimpleNamespace(free=100 * 1024 ** 3),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.purge_transient_artifacts",
                side_effect=purge,
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
            pinned_source_context,
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.run_sampled_calibration",
                side_effect=run_calibration,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._current_derivation_review_artifact_fingerprint",
                return_value=calibration_payload["review_artifact_fingerprint"],
            ),
            activity_guard,
        ):
            attempt = _run_av1_validation_derivation_assignment_locked(
                config=self.runtime_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=self._matching_repository_identity,
                assignment_id=assignment.assignment_id,
                attempts_directory=attempts_directory,
                terminal_records_directory=terminal_records_directory,
                process_controller=controller,
                deps=deps,
                _preflight=preflight,
                _source_commitment_guard=lambda: None,
            )

        terminal = load_av1_validation_derivation_terminal_records(
            terminal_records_directory
        )[0]
        return attempt, terminal

    def _assert_assignment_runtime_dependency_failure(
            self,
            *,
            dependency: str,
            reason_code: str,
    ) -> None:
        attempt, terminal = self._run_assignment_with_runtime_and_cleanup_result(
            failure_dependency=dependency,
            cleanup_failure=False,
        )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.reason_code, reason_code)
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.reason_code, reason_code)
        self.assertNotIn(
            "private runtime failure detail",
            canonical_json_bytes(attempt.to_payload()).decode("utf-8"),
        )

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

    def test_plan_capacity_gate_requires_all_authorized_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = (
                Path(directory)
                / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
                / self.plan.partition_id
            )
            total_source_size_bytes = sum(
                commitment.source_size_bytes
                for commitment in self.plan.source_commitments
            )
            required_free_bytes = (
                total_source_size_bytes
                + AV1_VALIDATION_DERIVATION_MINIMUM_FREE_BYTES
            )
            with (
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation._available_bytes_for_descriptor",
                    return_value=required_free_bytes - 1,
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "cumulative source snapshot capacity",
                ),
            ):
                assert_av1_validation_derivation_source_snapshot_capacity(
                    artifact_root,
                    plan=self.plan,
                )
            self.assertFalse((artifact_root / "plan.json").exists())
            snapshot_root = artifact_root / "source-snapshots"
            self.assertTrue(snapshot_root.is_dir())
            self.assertEqual(stat.S_IMODE(snapshot_root.stat().st_mode), 0o700)

            with patch(
                "mediaforce.web.runtime.av1_validation_derivation._available_bytes_for_descriptor",
                return_value=required_free_bytes,
            ):
                assert_av1_validation_derivation_source_snapshot_capacity(
                    artifact_root,
                    plan=self.plan,
                )

            commitment = self.plan.source_commitments[0]
            unexpected_path = (
                snapshot_root / f"{commitment.assignment_id}.source-media"
            )
            unexpected_path.write_bytes(b"pre-authorization-residue")
            unexpected_path.chmod(0o400)
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "unexpected pre-authorization artifacts",
            ):
                assert_av1_validation_derivation_source_snapshot_capacity(
                    artifact_root,
                    plan=self.plan,
                )

    def test_plan_capacity_retry_credits_matching_retained_snapshot(self) -> None:
        snapshot_root = self.runtime_artifact_root / "source-snapshots"
        snapshot_root.mkdir(mode=0o700)
        commitment = self.plan.source_commitments[0]
        snapshot_path = snapshot_root / f"{commitment.assignment_id}.source-media"
        with snapshot_path.open("wb") as snapshot_file:
            snapshot_file.truncate(commitment.source_size_bytes)
        snapshot_path.chmod(0o400)
        total_source_size_bytes = sum(
            item.source_size_bytes
            for item in self.plan.source_commitments
        )
        required_free_bytes = (
            total_source_size_bytes
            - commitment.source_size_bytes
            + AV1_VALIDATION_DERIVATION_MINIMUM_FREE_BYTES
        )

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.descriptor_content_version_fingerprint",
                return_value=commitment.source_identity,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.descriptor_sha256",
                return_value=commitment.source_sha256,
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._available_bytes_for_descriptor",
                side_effect=(required_free_bytes, required_free_bytes - 1),
            ),
        ):
            assert_av1_validation_derivation_source_snapshot_capacity(
                self.runtime_artifact_root,
                plan=self.plan,
            )
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "cumulative source snapshot capacity",
            ):
                assert_av1_validation_derivation_source_snapshot_capacity(
                    self.runtime_artifact_root,
                    plan=self.plan,
                )

    def test_plan_capacity_retry_rejects_unexpected_snapshot_entry(self) -> None:
        snapshot_root = self.runtime_artifact_root / "source-snapshots"
        snapshot_root.mkdir(mode=0o700)
        unexpected_path = snapshot_root / "unexpected.source-media"
        unexpected_path.write_bytes(b"unexpected-private-residue")
        unexpected_path.chmod(0o400)

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "unexpected artifacts",
        ):
            assert_av1_validation_derivation_source_snapshot_capacity(
                self.runtime_artifact_root,
                plan=self.plan,
            )

    def test_plan_capacity_retry_rejects_snapshot_identity_drift(self) -> None:
        snapshot_root = self.runtime_artifact_root / "source-snapshots"
        snapshot_root.mkdir(mode=0o700)
        commitment = self.plan.source_commitments[0]
        snapshot_path = snapshot_root / f"{commitment.assignment_id}.source-media"
        snapshot_path.write_bytes(b"partial-private-residue")
        snapshot_path.chmod(0o400)

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "retained source snapshot identity drifted",
        ):
            assert_av1_validation_derivation_source_snapshot_capacity(
                self.runtime_artifact_root,
                plan=self.plan,
            )

    def test_plan_capacity_retry_rejects_missing_snapshot_directory(self) -> None:
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "source-snapshot directory could not be safely bound",
        ):
            assert_av1_validation_derivation_source_snapshot_capacity(
                self.runtime_artifact_root,
                plan=self.plan,
            )

    def test_plan_capacity_gate_detects_snapshot_directory_swap(self) -> None:
        snapshot_root = self.runtime_artifact_root / "source-snapshots"
        snapshot_root.mkdir(mode=0o700)
        moved_snapshot_root = self.runtime_artifact_root / "moved-source-snapshots"
        total_source_size_bytes = sum(
            commitment.source_size_bytes
            for commitment in self.plan.source_commitments
        )

        def swap_snapshot_directory(_descriptor: int) -> int:
            snapshot_root.rename(moved_snapshot_root)
            snapshot_root.mkdir(mode=0o700)
            return (
                total_source_size_bytes
                + AV1_VALIDATION_DERIVATION_MINIMUM_FREE_BYTES
            )

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation._available_bytes_for_descriptor",
                side_effect=swap_snapshot_directory,
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "source-snapshot directory binding drifted",
            ),
        ):
            assert_av1_validation_derivation_source_snapshot_capacity(
                self.runtime_artifact_root,
                plan=self.plan,
            )

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
        self.assertTrue(
            set(_AV1_VALIDATION_DERIVATION_IMPLEMENTATION_FIXED_FILES).issubset({
                os.fsdecode(path)
                for path in (
                    verify_av1_cold_start_preregistration._BOUND_MEDIAFORCE_SNAPSHOT_AUXILIARY_PATHS
                )
            })
        )
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
                    operation_kwargs: dict[str, object] = {
                        "config_path": Path("unused.toml"),
                        "manifest": self.manifest,
                        "partition": self.partition,
                        "token_key": self.token_key,
                        "plan_path": aliased_artifact_root / "plan.json",
                        "cell_plan_id": self.plan.assignments[0].cell_plan_id,
                    }
                    if operation is finalize_runtime_av1_validation_derivation_candidate_lock:
                        operation_kwargs["repository_identity_resolver"] = (
                            self._matching_repository_identity
                        )
                    else:
                        operation_kwargs["repository_commit"] = (
                            self.plan.repository_commit
                        )
                        operation_kwargs["repository_tree"] = (
                            self.plan.repository_tree
                        )
                    operation(
                        **operation_kwargs,
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
                repository_identity_resolver=self._matching_repository_identity,
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
                repository_identity_resolver=self._matching_repository_identity,
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

    def test_preregistration_import_guard_rejects_repository_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            (repository / "mediaforce" / "__pycache__").mkdir(parents=True)
            (repository / "scripts").mkdir()

            with self.assertRaisesRegex(
                RuntimeError,
                "refuses repository bytecode caches",
            ):
                verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                    repository
                )

    def test_preregistration_bootstrap_rejects_nonisolated_startup(self) -> None:
        runner = Path(verify_av1_cold_start_preregistration.__file__).resolve()

        completed = subprocess.run(
            [sys.executable, runner, "--help"],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "must start with Python -I -S",
            completed.stderr,
        )

    def test_preregistration_bootstrap_rejects_shadow_module_before_import(self) -> None:
        source_runner = Path(
            verify_av1_cold_start_preregistration.__file__
        ).resolve()
        for ignored in (False, True):
            with self.subTest(ignored=ignored), tempfile.TemporaryDirectory() as directory:
                repository = Path(directory)
                scripts_directory = repository / "scripts"
                scripts_directory.mkdir()
                runner = scripts_directory / source_runner.name
                shutil.copyfile(source_runner, runner)
                if ignored:
                    (repository / ".gitignore").write_text(
                        "scripts/argparse.py\n",
                        encoding="utf-8",
                    )
                _run_test_git(repository, "init", "-q")
                paths_to_add = ["scripts"]
                if ignored:
                    paths_to_add.append(".gitignore")
                _run_test_git(repository, "add", *paths_to_add)
                _run_test_git(
                    repository,
                    "commit",
                    "-qm",
                    "bootstrap fixture",
                )
                sentinel = repository / "shadow-imported"
                (scripts_directory / "argparse.py").write_text(
                    "from pathlib import Path\n"
                    f"Path({str(sentinel)!r}).write_text('imported')\n",
                    encoding="utf-8",
                )

                completed = subprocess.run(
                    [sys.executable, "-I", "-S", runner, "--help"],
                    cwd=repository,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "refuses untracked or ignored import state",
                    completed.stderr,
                )
                self.assertFalse(sentinel.exists())

    def test_preregistration_bootstrap_pins_canonical_git_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            scripts_directory = repository / "scripts"
            mediaforce_directory = repository / "mediaforce"
            scripts_directory.mkdir()
            mediaforce_directory.mkdir()
            runner = scripts_directory / Path(
                verify_av1_cold_start_preregistration.__file__
            ).name
            shutil.copyfile(
                Path(verify_av1_cold_start_preregistration.__file__),
                runner,
            )
            shadow_module = scripts_directory / "argparse.py"
            shadow_module.write_text("VALUE = 'clean'\n", encoding="utf-8")
            (mediaforce_directory / "__init__.py").write_text(
                '"""fixture"""\n',
                encoding="utf-8",
            )
            for arguments in (
                ("init", "-q"),
                ("add", "mediaforce", "scripts"),
                ("commit", "-qm", "bootstrap fixture"),
            ):
                _run_test_git(repository, *arguments)
            alternate_worktree = repository / "alternate-worktree"
            shutil.copytree(scripts_directory, alternate_worktree / "scripts")
            shutil.copytree(mediaforce_directory, alternate_worktree / "mediaforce")
            _run_test_git(
                repository,
                "config",
                "core.worktree",
                str(alternate_worktree),
            )
            shadow_module.write_text("VALUE = 'modified'\n", encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, "-I", "-S", runner, "--help"],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "refuses modified import state",
                completed.stderr,
            )

    def test_preregistration_bootstrap_rejects_transient_git_directory_swap(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            (repository / "scripts").mkdir(parents=True)
            (repository / "mediaforce").mkdir()
            (repository / "scripts" / "runner.py").write_text(
                "VALUE = 'tracked'\n",
                encoding="utf-8",
            )
            (repository / "mediaforce" / "__init__.py").write_text(
                '"""fixture"""\n',
                encoding="utf-8",
            )
            _run_test_git(repository, "init", "-q")
            _run_test_git(repository, "add", "mediaforce", "scripts")
            _run_test_git(
                repository,
                "commit",
                "-qm",
                "transient authority fixture",
            )
            sentinel = Path(directory) / "git-probe-started"
            swapper = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    (
                        "import pathlib,sys,time; "
                        "repository=pathlib.Path(sys.argv[1]); "
                        "sentinel=pathlib.Path(sys.argv[2]); "
                        "deadline=time.monotonic()+5; "
                        "exec('while not sentinel.exists():\\n "
                        "   assert time.monotonic() < deadline\\n "
                        "   time.sleep(0.01)'); "
                        "original=repository/'.git-original'; "
                        "metadata=repository/'.git'; "
                        "metadata.rename(original); metadata.mkdir(); "
                        "time.sleep(0.05); metadata.rmdir(); "
                        "original.rename(metadata)"
                    ),
                    str(repository),
                    str(sentinel),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            real_execve = os.execve

            def delayed_execve(
                    path: str,
                    arguments: tuple[str, ...],
                    environment: dict[str, str],
            ) -> None:
                sentinel.touch()
                time.sleep(0.3)
                real_execve(path, arguments, environment)

            try:
                with (
                    patch.object(os, "execve", new=delayed_execve),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "detected changed Git metadata",
                    ),
                ):
                    verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                        repository
                    )
            finally:
                try:
                    _stdout, stderr = swapper.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    swapper.kill()
                    _stdout, stderr = swapper.communicate()
                self.assertEqual(swapper.returncode, 0, stderr)

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "requires kqueue or inotify",
    )
    def test_preregistration_bootstrap_rejects_package_swap_after_last_probe(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            scripts_directory = repository / "scripts"
            package_directory = repository / "mediaforce"
            scripts_directory.mkdir(parents=True)
            package_directory.mkdir()
            (scripts_directory / "runner.py").write_text(
                "VALUE = 'tracked'\n",
                encoding="utf-8",
            )
            (package_directory / "__init__.py").write_text(
                '"""fixture"""\n',
                encoding="utf-8",
            )
            for arguments in (
                ("init", "-q"),
                ("add", "mediaforce", "scripts"),
                ("commit", "-qm", "bootstrap package fixture"),
            ):
                _run_test_git(repository, *arguments)

            monitor = verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                repository,
                retain_authority_monitor=True,
            )
            self.assertIsInstance(
                monitor,
                verify_av1_cold_start_preregistration._RepositoryAuthorityMonitor,
            )
            assert isinstance(
                monitor,
                verify_av1_cold_start_preregistration._RepositoryAuthorityMonitor,
            )
            original_package = repository / "mediaforce-original"
            try:
                package_directory.rename(original_package)
                package_directory.mkdir()
                package_directory.rmdir()
                original_package.rename(package_directory)

                with self.assertRaisesRegex(
                    RuntimeError,
                    "detected changed Git metadata",
                ):
                    monitor.assert_quiet()
            finally:
                monitor.close()

    def test_preregistration_bootstrap_rejects_foreign_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            foreign_repository = root / "foreign-repository"
            for checkout in (repository, foreign_repository):
                (checkout / "scripts").mkdir(parents=True)
                (checkout / "mediaforce").mkdir()
                (checkout / "scripts" / "runner.py").write_text(
                    "VALUE = 'tracked'\n",
                    encoding="utf-8",
                )
                (checkout / "mediaforce" / "__init__.py").write_text(
                    '"""fixture"""\n',
                    encoding="utf-8",
                )
            for arguments in (
                ("init", "-q"),
                ("add", "mediaforce", "scripts"),
                ("commit", "-qm", "foreign fixture"),
            ):
                _run_test_git(foreign_repository, *arguments)
            (repository / ".git").write_text(
                f"gitdir: {foreign_repository / '.git'}\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "could not inspect Git worktree backlink",
            ):
                verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                    repository
                )

    def test_preregistration_bootstrap_rejects_foreign_git_directory_symlink(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            foreign_repository = root / "foreign-repository"
            for checkout in (repository, foreign_repository):
                (checkout / "scripts").mkdir(parents=True)
                (checkout / "mediaforce").mkdir()
                (checkout / "scripts" / "runner.py").write_text(
                    "VALUE = 'tracked'\n",
                    encoding="utf-8",
                )
                (checkout / "mediaforce" / "__init__.py").write_text(
                    '"""fixture"""\n',
                    encoding="utf-8",
                )
            for arguments in (
                ("init", "-q"),
                ("add", "mediaforce", "scripts"),
                ("commit", "-qm", "foreign fixture"),
            ):
                _run_test_git(foreign_repository, *arguments)
            (repository / ".git").symlink_to(
                foreign_repository / ".git",
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "refuses unsafe Git metadata",
            ):
                verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                    repository
                )

    def test_preregistration_bootstrap_accepts_linked_worktree_git_directory(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            linked_worktree = root / "linked-worktree"
            (repository / "scripts").mkdir(parents=True)
            (repository / "mediaforce").mkdir()
            (repository / "scripts" / "runner.py").write_text(
                "VALUE = 'tracked'\n",
                encoding="utf-8",
            )
            (repository / "mediaforce" / "__init__.py").write_text(
                '"""fixture"""\n',
                encoding="utf-8",
            )
            for arguments in (
                ("init", "-q"),
                ("add", "mediaforce", "scripts"),
                ("commit", "-qm", "linked fixture"),
            ):
                _run_test_git(repository, *arguments)
            _run_test_git(
                repository,
                "worktree",
                "add",
                "--detach",
                "-q",
                str(linked_worktree),
                "HEAD",
            )

            verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                linked_worktree
            )

    def test_preregistration_bootstrap_raw_bytes_do_not_execute_filters(self) -> None:
        for filter_kind in ("clean", "process"):
            with self.subTest(filter_kind=filter_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository = root / "repository"
                _initialize_preregistration_test_repository(repository)
                sentinel = root / f"{filter_kind}-filter-executed"
                filter_command = root / f"hostile-{filter_kind}.sh"
                filter_command.write_text(
                    "#!/bin/sh\n"
                    f": > {str(sentinel)!r}\n"
                    + ("cat\n" if filter_kind == "clean" else "exit 1\n"),
                    encoding="utf-8",
                )
                filter_command.chmod(0o700)
                info_directory = repository / ".git" / "info"
                info_directory.mkdir(exist_ok=True)
                (info_directory / "attributes").write_text(
                    "mediaforce/module.py filter=hostile\n",
                    encoding="utf-8",
                )
                _run_test_git(
                    repository,
                    "config",
                    "--local",
                    f"filter.hostile.{filter_kind}",
                    str(filter_command),
                )
                (repository / "mediaforce" / "module.py").write_text(
                    "VALUE = 'dirty'\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    RuntimeError,
                    "refuses modified import state",
                ):
                    verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                        repository
                    )

                self.assertFalse(sentinel.exists())

    def test_preregistration_bootstrap_git_environment_disables_lazy_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            _initialize_preregistration_test_repository(repository)
            recorded_values = root / "lazy-fetch-values"
            real_execve = os.execve

            def record_execve(
                    path: str,
                    arguments: tuple[str, ...],
                    environment: dict[str, str],
            ) -> None:
                with recorded_values.open("a", encoding="utf-8") as handle:
                    handle.write(f"{environment['GIT_NO_LAZY_FETCH']}\n")
                real_execve(path, arguments, environment)

            with patch.object(os, "execve", new=record_execve):
                verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                    repository
                )

            values = recorded_values.read_text(encoding="utf-8").splitlines()
            self.assertTrue(values)
            self.assertEqual(set(values), {"1"})

    def test_git_authority_rejects_local_and_worktree_config_includes(self) -> None:
        for config_kind in ("local", "worktree"):
            with self.subTest(config_kind=config_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository = root / "repository"
                _initialize_preregistration_test_repository(repository)
                external_config = root / "external-config"
                external_config.write_text(
                    "[core]\n\tfilemode = false\n",
                    encoding="utf-8",
                )
                include_payload = (
                    "[include]\n"
                    f"\tpath = {external_config}\n"
                )
                if config_kind == "local":
                    with (repository / ".git" / "config").open(
                        "a",
                        encoding="utf-8",
                    ) as handle:
                        handle.write(include_payload)
                else:
                    _run_test_git(
                        repository,
                        "config",
                        "--local",
                        "extensions.worktreeConfig",
                        "true",
                    )
                    (repository / ".git" / "config.worktree").write_text(
                        include_payload,
                        encoding="utf-8",
                    )

                for guard_name, guard in (
                    (
                        "bootstrap",
                        lambda: verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                            repository
                        ),
                    ),
                    (
                        "runtime",
                        lambda: verify_av1_cold_start_preregistration._review_git_environment(
                            repository_root=repository
                        ),
                    ),
                ):
                    with self.subTest(guard=guard_name), self.assertRaisesRegex(
                        (RuntimeError, AV1ValidationDerivationError),
                        "external or symlinked Git authority",
                    ):
                        guard()

    def test_git_authority_rejects_local_and_worktree_promisor_config(self) -> None:
        for config_kind in ("local", "worktree"):
            for configuration in (
                    "[extensions]\n\tpartialClone = origin\n",
                    "[remote \"origin\"]\n\tpromisor = true\n",
                    "[remote.origin]\n\tpromisor = true\n",
                    "[remote.origin]\n\tpartialCloneFilter = blob:none\n",
            ):
                with self.subTest(
                        config_kind=config_kind,
                        configuration=configuration,
                ), tempfile.TemporaryDirectory() as directory:
                    repository = Path(directory) / "repository"
                    _initialize_preregistration_test_repository(repository)
                    if config_kind == "local":
                        with (repository / ".git" / "config").open(
                                "a",
                                encoding="utf-8",
                        ) as handle:
                            handle.write(configuration)
                    else:
                        _run_test_git(
                            repository,
                            "config",
                            "--local",
                            "extensions.worktreeConfig",
                            "true",
                        )
                        (repository / ".git" / "config.worktree").write_text(
                            configuration,
                            encoding="utf-8",
                        )

                    for guard in (
                        lambda: verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                            repository
                        ),
                        lambda: verify_av1_cold_start_preregistration._review_git_environment(
                            repository_root=repository
                        ),
                    ):
                        with self.assertRaisesRegex(
                            (RuntimeError, AV1ValidationDerivationError),
                            "external or symlinked Git authority",
                        ):
                            guard()

    def test_git_authority_rejects_nonempty_object_alternates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            _initialize_preregistration_test_repository(repository)
            alternate_objects = root / "alternate-objects"
            alternate_objects.mkdir()
            alternates = repository / ".git" / "objects" / "info" / "alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(f"{alternate_objects}\n", encoding="utf-8")

            for guard in (
                lambda: verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                    repository
                ),
                lambda: verify_av1_cold_start_preregistration._review_git_environment(
                    repository_root=repository
                ),
            ):
                with self.assertRaisesRegex(
                    (RuntimeError, AV1ValidationDerivationError),
                    "external or symlinked Git authority",
                ):
                    guard()

    def test_git_authority_rejects_nested_metadata_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            _initialize_preregistration_test_repository(repository)
            external_ref = root / "external-ref"
            external_ref.write_text("ref: refs/heads/main\n", encoding="utf-8")
            (repository / ".git" / "refs" / "authority-link").symlink_to(
                external_ref
            )

            for guard in (
                lambda: verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                    repository
                ),
                lambda: verify_av1_cold_start_preregistration._review_git_environment(
                    repository_root=repository
                ),
            ):
                with self.assertRaisesRegex(
                    (RuntimeError, AV1ValidationDerivationError),
                    "external or symlinked Git authority",
                ):
                    guard()

    def test_preregistration_bootstrap_authority_failure_is_sticky(self) -> None:
        class OneShotFailureMonitor:
            def __init__(self) -> None:
                self.calls = 0

            def assert_quiet(self) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient authority failure")

        monitor = OneShotFailureMonitor()
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_PREREGISTRATION_BOOTSTRAP_ACTIVE",
                True,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_PREREGISTRATION_BOOTSTRAP_AUTHORITY_MONITOR",
                monitor,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_PREREGISTRATION_BOOTSTRAP_FAILURE",
                None,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "changed repository authority"):
                verify_av1_cold_start_preregistration._assert_preregistration_bootstrap_authority()
            with self.assertRaisesRegex(RuntimeError, "previously failed"):
                verify_av1_cold_start_preregistration._assert_preregistration_bootstrap_authority()
        self.assertEqual(monitor.calls, 1)

    def test_validate_output_rechecks_bootstrap_authority_before_print(self) -> None:
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_preregistration_bootstrap_authority",
                side_effect=(None, RuntimeError("sticky authority failure")),
            ),
            patch("builtins.print") as print_mock,
            self.assertRaisesRegex(RuntimeError, "sticky authority failure"),
        ):
            verify_av1_cold_start_preregistration.main(
                ["validate", str(V2_MANIFEST_PATH), "--json"]
            )
        print_mock.assert_not_called()

    def test_preregistration_bootstrap_rejects_unapproved_venv_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            scripts_directory = repository / "scripts"
            mediaforce_directory = repository / "mediaforce"
            scripts_directory.mkdir()
            mediaforce_directory.mkdir()
            runner = scripts_directory / Path(
                verify_av1_cold_start_preregistration.__file__
            ).name
            shutil.copyfile(
                Path(verify_av1_cold_start_preregistration.__file__),
                runner,
            )
            (mediaforce_directory / "__init__.py").write_text(
                '"""fixture"""\n',
                encoding="utf-8",
            )
            for arguments in (
                ("init", "-q"),
                ("add", "mediaforce", "scripts"),
                ("commit", "-qm", "launcher fixture"),
            ):
                _run_test_git(repository, *arguments)
            virtual_environment = repository / ".venv"
            launcher = virtual_environment / "bin" / "python-unapproved"
            launcher.parent.mkdir(parents=True)
            launcher.symlink_to(sys._base_executable)
            (virtual_environment / "bin" / "python").symlink_to(
                sys._base_executable
            )
            (virtual_environment / "pyvenv.cfg").write_text(
                f"home = {Path(sys._base_executable).parent}\n"
                "include-system-site-packages = false\n"
                f"version = {sys.version_info.major}.{sys.version_info.minor}\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [launcher, "-I", "-S", runner, "--help"],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "requires the canonical repository virtual environment",
                completed.stderr,
            )

    def test_preregistration_executable_trusts_only_safe_current_or_root_owner(
            self,
    ) -> None:
        current_uid = 501
        regular_mode = stat.S_IFREG | 0o755
        cases = (
            ("current owner", current_uid, regular_mode, True),
            ("root owner", 0, regular_mode, True),
            ("root group writable", 0, stat.S_IFREG | 0o775, False),
            (
                "current owner group writable",
                current_uid,
                stat.S_IFREG | 0o775,
                False,
            ),
            ("foreign owner", current_uid + 1, regular_mode, False),
            ("world writable", current_uid, stat.S_IFREG | 0o757, False),
            ("setuid", 0, stat.S_IFREG | 0o4755, False),
            ("setgid", 0, stat.S_IFREG | 0o2755, False),
            ("not regular", current_uid, stat.S_IFDIR | 0o755, False),
        )
        for label, owner_uid, mode, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(
                    verify_av1_cold_start_preregistration
                    ._preregistration_executable_is_trusted(
                        owner_uid=owner_uid,
                        mode=mode,
                        current_uid=current_uid,
                    ),
                    expected,
                )
        self.assertEqual(
            verify_av1_cold_start_preregistration
            ._preregistration_executable_rejection_reasons(
                owner_uid=current_uid + 1,
                mode=stat.S_IFREG | 0o6777,
                current_uid=current_uid,
            ),
            (
                "executable_owner",
                "executable_world_write",
                "executable_set_id",
                "executable_group_write",
            ),
        )

    def test_review_git_uses_root_owned_direct_system_binary(self) -> None:
        expected = (
            "/Library/Developer/CommandLineTools/usr/bin/git"
            if sys.platform == "darwin"
            else "/usr/bin/git"
        )
        command = verify_av1_cold_start_preregistration._review_git_command(
            "--version"
        )
        self.assertEqual(command[0], expected)
        binary_info = Path(expected).lstat()
        self.assertTrue(stat.S_ISREG(binary_info.st_mode))
        self.assertEqual(binary_info.st_uid, 0)
        self.assertFalse(
            binary_info.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
        )
        self.assertTrue(
            verify_av1_cold_start_preregistration
            ._system_git_binary_is_trusted(expected)
        )

    def test_preregistration_bootstrap_rejects_hostile_virtual_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            scripts_directory = repository / "scripts"
            mediaforce_directory = repository / "mediaforce"
            scripts_directory.mkdir()
            mediaforce_directory.mkdir()
            runner = scripts_directory / Path(
                verify_av1_cold_start_preregistration.__file__
            ).name
            shutil.copyfile(
                Path(verify_av1_cold_start_preregistration.__file__),
                runner,
            )
            (mediaforce_directory / "__init__.py").write_text(
                "import sqlalchemy\n",
                encoding="utf-8",
            )
            for arguments in (
                ("init", "-q"),
                ("add", "mediaforce", "scripts"),
                ("commit", "-qm", "bootstrap fixture"),
            ):
                _run_test_git(repository, *arguments)
            canonical_python = repository / ".venv" / "bin" / "python3"
            canonical_python.parent.mkdir(parents=True)
            canonical_python.symlink_to(sys.executable)
            hostile_environment = repository / "hostile-venv"
            hostile_package = (
                hostile_environment
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
                / "sqlalchemy"
            )
            hostile_package.mkdir(parents=True)
            sentinel = repository / "hostile-dependency-imported"
            (hostile_package / "__init__.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('imported')\n",
                encoding="utf-8",
            )
            child_environment = dict(os.environ)
            child_environment["VIRTUAL_ENV"] = str(hostile_environment)

            completed = subprocess.run(
                [canonical_python, "-I", "-S", runner, "--help"],
                cwd=repository,
                env=child_environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "requires the canonical repository virtual environment",
                completed.stderr,
            )
            self.assertFalse(sentinel.exists())

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "requires kqueue or inotify",
    )
    def test_preregistration_imports_only_bound_source_bytes_during_swap_restore(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            scripts_directory = repository / "scripts"
            scripts_directory.mkdir(parents=True)
            source_root = Path(
                verify_av1_cold_start_preregistration.__file__
            ).resolve().parents[1]
            shutil.copytree(
                source_root / "mediaforce",
                repository / "mediaforce",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            runner = scripts_directory / Path(
                verify_av1_cold_start_preregistration.__file__
            ).name
            shutil.copyfile(
                Path(verify_av1_cold_start_preregistration.__file__),
                runner,
            )
            import_started = root / "bound-import-started"
            package_init = repository / "mediaforce" / "__init__.py"
            package_source = package_init.read_text(encoding="utf-8")
            package_init.write_text(
                "from pathlib import Path as _BoundTestPath\n"
                "import time as _bound_test_time\n"
                f"_BoundTestPath({str(import_started)!r}).write_text('ready')\n"
                "_bound_test_time.sleep(1.0)\n\n"
                f"{package_source}",
                encoding="utf-8",
            )
            for arguments in (
                ("init", "-q"),
                ("add", "mediaforce", "scripts"),
                ("commit", "-qm", "bound import lifecycle"),
            ):
                _run_test_git(repository, *arguments)

            canonical_python = (
                _create_preregistration_fixture_virtual_environment(repository)
            )
            malicious_executed = root / "substituted-module-executed"
            target_module = repository / "mediaforce" / "core" / "config.py"
            original_source = target_module.read_bytes()
            substituted_source = (
                "from pathlib import Path\n"
                f"Path({str(malicious_executed)!r}).write_text('executed')\n"
                "raise RuntimeError('substituted source executed')\n"
            ).encode("utf-8")
            child_environment = dict(os.environ)
            child_environment.pop("PYTHONPATH", None)
            canonical_repository = repository.resolve()
            canonical_launcher = canonical_repository / ".venv" / "bin" / "python"
            child_environment["VIRTUAL_ENV"] = str(canonical_repository / ".venv")
            process = subprocess.Popen(
                [
                    canonical_launcher,
                    "-I",
                    "-S",
                    canonical_repository / "scripts" / runner.name,
                    "--help",
                ],
                cwd=canonical_repository,
                env=child_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 20
                while not import_started.exists() and process.poll() is None:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
                if not import_started.exists():
                    stdout, stderr = process.communicate(timeout=5)
                    self.fail(
                        "bound-source import fixture did not start: "
                        f"stdout={stdout!r} stderr={stderr!r}"
                    )
                target_module.write_bytes(substituted_source)
                time.sleep(0.1)
                target_module.write_bytes(original_source)
                stdout, stderr = process.communicate(timeout=30)
            finally:
                target_module.write_bytes(original_source)
                if process.poll() is None:
                    process.kill()
                    process.communicate()

            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn("changed repository authority", stderr)
            self.assertFalse(malicious_executed.exists())

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "requires kqueue or inotify",
    )
    def test_preregistration_snapshot_binds_resources_helpers_and_source_defaults(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            source_root = Path(
                verify_av1_cold_start_preregistration.__file__
            ).resolve().parents[1]
            shutil.copytree(
                source_root / "mediaforce",
                repository / "mediaforce",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            (repository / "scripts").mkdir()
            shutil.copyfile(
                source_root / "scripts" / "verify_av1_cold_start_preregistration.py",
                repository / "scripts" / "verify_av1_cold_start_preregistration.py",
            )
            (repository / "config").mkdir()
            shutil.copyfile(
                source_root / "config" / "defaults.toml",
                repository / "config" / "defaults.toml",
            )
            for filename in ("hatch_build.py", "pyproject.toml", "uv.lock"):
                shutil.copyfile(source_root / filename, repository / filename)
            for arguments in (
                ("init", "-q"),
                (
                    "add",
                    "config/defaults.toml",
                    "hatch_build.py",
                    "mediaforce",
                    "pyproject.toml",
                    "scripts",
                    "uv.lock",
                ),
                ("commit", "-qm", "bound snapshot fixture"),
            ):
                _run_test_git(repository, *arguments)
            self.assertEqual(_run_test_git(repository, "config", "--get", "gc.auto"), "0")
            self.assertEqual(
                _run_test_git(repository, "config", "--get", "maintenance.auto"),
                "false",
            )

            canonical_repository = repository.resolve()
            canonical_python = (
                _create_preregistration_fixture_virtual_environment(
                    canonical_repository
                )
            )
            child_environment = dict(os.environ)
            child_environment.pop("PYTHONPATH", None)
            child_environment["VIRTUAL_ENV"] = str(
                canonical_repository / ".venv"
            )
            completed = subprocess.run(
                [
                    canonical_python,
                    "-I",
                    "-S",
                    canonical_repository
                    / "scripts"
                    / "verify_av1_cold_start_preregistration.py",
                    "--help",
                ],
                cwd=canonical_repository,
                env=child_environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Validate an AV1 cold-start preregistration", completed.stdout)

            monitor = verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                repository,
                retain_authority_monitor=True,
            )
            self.assertIsInstance(
                monitor,
                verify_av1_cold_start_preregistration._RepositoryAuthorityMonitor,
            )
            assert isinstance(
                monitor,
                verify_av1_cold_start_preregistration._RepositoryAuthorityMonitor,
            )
            snapshot = Path(monitor.bound_snapshot_directory())
            self.assertNotEqual(
                os.path.commonpath((repository.resolve(), snapshot)),
                str(repository.resolve()),
            )
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o700)
            source_map = monitor.bound_python_sources()
            self.assertEqual(
                Path(source_map["mediaforce.core.config"][0]),
                snapshot / "mediaforce" / "core" / "config.py",
            )

            authority_paths = (
                Path("config/defaults.toml"),
                Path("mediaforce/core/_process_deadline.py"),
                Path("mediaforce/core/db_migration_scripts/env.py"),
                Path("mediaforce/core/sql/schema.sql"),
                Path("mediaforce/package_defaults/defaults.toml"),
                Path("pyproject.toml"),
                Path("scripts/verify_av1_cold_start_preregistration.py"),
                Path("uv.lock"),
            )
            original_payloads = {
                relative_path: (repository / relative_path).read_bytes()
                for relative_path in authority_paths
            }
            saved_modules = {
                name: module
                for name, module in sys.modules.items()
                if name == "mediaforce" or name.startswith("mediaforce.")
            }
            saved_meta_path = list(sys.meta_path)
            bound_config = None
            bound_migrations = None
            bound_process_control = None
            try:
                for name in tuple(saved_modules):
                    del sys.modules[name]
                verify_av1_cold_start_preregistration._install_bound_mediaforce_importer(
                    monitor
                )
                bound_config = import_module("mediaforce.core.config")
                bound_migrations = import_module("mediaforce.core.db_migrations")
                bound_process_control = import_module("mediaforce.core.process_control")
                for relative_path in authority_paths:
                    (repository / relative_path).write_bytes(
                        b"mutable checkout replacement\n"
                    )

                self.assertEqual(
                    Path(bound_config.__file__),
                    snapshot / "mediaforce" / "core" / "config.py",
                )
                self.assertEqual(
                    bound_config.DEFAULT_CONFIG_PATH,
                    snapshot / "config" / "defaults.toml",
                )
                self.assertEqual(
                    resources.files("mediaforce.package_defaults")
                    .joinpath("defaults.toml")
                    .read_bytes(),
                    original_payloads[Path("mediaforce/package_defaults/defaults.toml")],
                )
                self.assertEqual(
                    resources.files("mediaforce.core")
                    .joinpath("sql")
                    .joinpath("schema.sql")
                    .read_bytes(),
                    original_payloads[Path("mediaforce/core/sql/schema.sql")],
                )
                with bound_migrations._alembic_script_location() as script_location:
                    self.assertEqual(
                        Path(script_location),
                        snapshot / "mediaforce" / "core" / "db_migration_scripts",
                    )
                    self.assertEqual(
                        (Path(script_location) / "env.py").read_bytes(),
                        original_payloads[
                            Path("mediaforce/core/db_migration_scripts/env.py")
                        ],
                    )
                result = bound_process_control.run_command(
                    [sys.executable, "-c", "print('bound-helper')"],
                    process_controller=bound_process_control.ManagedProcessController(),
                )
                self.assertEqual(result.stdout, "bound-helper\n")
                self.assertEqual(
                    (snapshot / "config" / "defaults.toml").read_bytes(),
                    original_payloads[Path("config/defaults.toml")],
                )
                for relative_path in (
                    _AV1_VALIDATION_DERIVATION_IMPLEMENTATION_FIXED_FILES
                ):
                    self.assertEqual(
                        (snapshot / relative_path).read_bytes(),
                        original_payloads[Path(relative_path)],
                    )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "detected changed Git metadata",
                ):
                    monitor.assert_quiet()
            finally:
                for relative_path, payload in original_payloads.items():
                    (repository / relative_path).write_bytes(payload)
                sys.meta_path[:] = saved_meta_path
                for name in tuple(sys.modules):
                    if name == "mediaforce" or name.startswith("mediaforce."):
                        del sys.modules[name]
                sys.modules.update(saved_modules)
                monitor.close()
            self.assertFalse(snapshot.exists())

    def test_preregistration_bootstrap_rejects_intermediate_execution_symlink(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            _initialize_preregistration_test_repository(repository)
            external_tree = root / "external-tree"
            external_tree.mkdir()
            (repository / "mediaforce" / "nested").symlink_to(
                external_tree,
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "symlinked execution authority",
            ):
                verify_av1_cold_start_preregistration._assert_preregistration_import_tree_clean(
                    repository
                )

    def test_derivation_review_checks_environment_before_claim(self) -> None:
        proposal = self._candidate_proposal()
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
                return_value=proposal,
            ) as load_proposal,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_existing_derivation_review",
                return_value=None,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_run_code_llm_review",
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
        load_proposal.assert_called_once()
        run_review.assert_not_called()

    def test_derivation_status_reports_recovery_required_for_orphan_claim(self) -> None:
        assignment = self.plan.assignments[0]
        attempts_directory = self.runtime_artifact_root / "attempts"
        write_av1_validation_derivation_assignment_claim(
            attempts_directory,
            assignment_id=assignment.assignment_id,
            plan_id=self.plan.plan_id,
            authorization_id=self.plan.authorization.authorization_id,
            claimed_at="2026-07-28T01:00:00Z",
            published_before=self.plan.authorization.valid_until,
        )
        args = SimpleNamespace(
            action="derivation-status",
            config=Path("unused.toml"),
            plan=self.runtime_artifact_root / "plan.json",
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
                "_print_partition_payload",
            ) as print_payload,
        ):
            exit_code = (
                verify_av1_cold_start_preregistration._run_derivation_action_body(
                    args,
                    locked_config=None,
                )
            )

        self.assertEqual(exit_code, 2)
        payload = print_payload.call_args.args[0]
        self.assertTrue(payload["recovery_required"])
        self.assertEqual(payload["assignment_claim_count"], 1)
        self.assertEqual(payload["unresolved_assignment_claim_count"], 1)
        self.assertEqual(payload["attempt_count"], 0)
        self.assertEqual(payload["terminal_record_count"], 0)

    def test_derivation_status_reports_reason_code_counts_separately(self) -> None:
        assignment = self.plan.assignments[0]
        attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="failed",
            reason_code="runtime_quality_search_failure",
        )
        terminal = build_av1_validation_derivation_terminal_record(
            plan=self.plan,
            partition=self.partition,
            attempt=attempt,
        )
        write_av1_validation_derivation_attempt(
            self.runtime_artifact_root / "attempts",
            attempt,
        )
        write_av1_validation_derivation_terminal_record(
            self.runtime_artifact_root / "terminal-records",
            terminal,
        )
        args = SimpleNamespace(
            action="derivation-status",
            config=Path("unused.toml"),
            plan=self.runtime_artifact_root / "plan.json",
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
                "_print_partition_payload",
            ) as print_payload,
        ):
            exit_code = (
                verify_av1_cold_start_preregistration._run_derivation_action_body(
                    args,
                    locked_config=None,
                )
            )

        self.assertEqual(exit_code, 0)
        payload = print_payload.call_args.args[0]
        self.assertEqual(
            payload["attempt_reason_code_counts"],
            {"runtime_quality_search_failure": 1},
        )
        self.assertEqual(
            payload["terminal_reason_code_counts"],
            {"runtime_quality_search_failure": 1},
        )

    def test_run_assignment_cli_reports_privacy_safe_reason_code(self) -> None:
        assignment = self.plan.assignments[0]
        attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="failed",
            reason_code="runtime_crop_detection_failure",
        )
        args = SimpleNamespace(
            action="run-derivation-assignment",
            manifest=V2_MANIFEST_PATH,
            partition=self.runtime_artifact_root / "partition.json",
            plan=self.runtime_artifact_root / "plan.json",
            assignment_id=assignment.assignment_id,
            key=self.runtime_artifact_root / "partition.key",
            config=Path("unused.toml"),
            json_output=True,
        )
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_preregistration_bootstrap_authority",
            ),
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
                "run_av1_validation_derivation_assignment",
                return_value=attempt,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_print_partition_payload",
            ) as print_payload,
        ):
            exit_code = (
                verify_av1_cold_start_preregistration._run_derivation_action_body(
                    args,
                    locked_config=None,
                )
            )

        self.assertEqual(exit_code, 2)
        payload = print_payload.call_args.args[0]
        self.assertEqual(
            payload["reason_code"],
            "runtime_crop_detection_failure",
        )
        self.assertNotIn("private", canonical_json_bytes(payload).decode("utf-8"))

    def test_derivation_status_reports_unresolved_review_claim(self) -> None:
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
            review_run_id="80500000-0000-0000-0000-000000000001",
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
            action="derivation-status",
            config=Path("unused.toml"),
            plan=self.runtime_artifact_root / "plan.json",
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
                "_print_partition_payload",
            ) as print_payload,
        ):
            exit_code = (
                verify_av1_cold_start_preregistration._run_derivation_action_body(
                    args,
                    locked_config=None,
                )
            )

        self.assertEqual(exit_code, 2)
        payload = print_payload.call_args.args[0]
        self.assertTrue(payload["recovery_required"])
        self.assertEqual(payload["review_claim_count"], 1)
        self.assertEqual(payload["unresolved_review_claim_count"], 1)

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
                "_run_code_llm_review",
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

    def test_review_retry_reuses_unresolved_claim(self) -> None:
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
        evidence = build_av1_validation_derivation_review_recovery_evidence(
            proposal=proposal,
            claim=claim,
            reason_code="interrupted_before_durable_response",
            recovered_at="2026-07-28T03:30:00Z",
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
                "_repository_review_identity",
                return_value=(
                    self.plan.repository_commit,
                    self.plan.repository_tree,
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_run_code_llm_review",
                return_value=(
                    claim,
                    evidence,
                    "rejected",
                    "2026-07-28T03:30:00Z",
                ),
            ) as run_review,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_print_partition_payload",
            ),
        ):
            exit_code = (
                verify_av1_cold_start_preregistration._run_derivation_action_body(
                    args,
                    locked_config=self.runtime_config,
                )
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(run_review.call_args.kwargs["claim"], claim)
        recovered = load_av1_validation_derivation_review_envelope(
            self.runtime_artifact_root,
            plan=self.plan,
            proposal=proposal,
            claim=claim,
        )
        self.assertEqual(recovered.review.review_claim_id, claim.claim_id)
        self.assertEqual(recovered.review.decision, "rejected")
        self.assertEqual(recovered.review.reviewed_at, "2026-07-28T03:30:00Z")

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
                if directory_fsyncs == 2:
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
            self.assertFalse(artifact_path.exists())

    def test_derivation_plan_rejects_post_authorization_publication(self) -> None:
        artifact_root = (
            self.runtime_config.paths.web_state_dir.parent
            / "late-state"
            / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
            / self.plan.partition_id
        )
        plan_path = artifact_root / "plan.json"
        with patch(
            "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
            return_value=10 ** 30,
        ):
            with self.assertRaises(AV1ValidationDerivationPublicationDeadlineError):
                write_av1_validation_derivation_plan(artifact_root, self.plan)
            self.assertFalse(plan_path.exists())

    def test_review_pending_attempt_rejects_post_authorization_publication(self) -> None:
        attempts_directory = self.runtime_config.paths.web_state_dir / "late-attempts"
        attempt = self._review_pending_attempt()
        attempt_path = attempts_directory / f"{attempt.assignment_id}.json"
        with patch(
            "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
            return_value=10 ** 30,
        ):
            with self.assertRaises(AV1ValidationDerivationPublicationDeadlineError):
                write_av1_validation_derivation_attempt(
                    attempts_directory,
                    attempt,
                    published_before=self.plan.authorization.valid_until,
                )
            self.assertFalse(attempt_path.exists())
            before_publish = unittest.mock.Mock()
            with self.assertRaises(AV1ValidationDerivationPublicationDeadlineError):
                write_av1_validation_derivation_attempt(
                    attempts_directory,
                    attempt,
                    before_publish=before_publish,
                    published_before=self.plan.authorization.valid_until,
                )
            before_publish.assert_called_once_with()
            self.assertFalse(attempt_path.exists())
            self.assertEqual(
                load_av1_validation_derivation_attempts(
                    attempts_directory,
                    review_pending_published_before=(
                        self.plan.authorization.valid_until
                    ),
                ),
                (),
            )

    def test_attempt_post_publish_identity_failure_removes_artifact(self) -> None:
        attempts_directory = self.runtime_config.paths.web_state_dir / "drifted-attempts"
        attempt = self._review_pending_attempt()
        attempt_path = attempts_directory / f"{attempt.assignment_id}.json"
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "repository drift",
        ):
            write_av1_validation_derivation_attempt(
                attempts_directory,
                attempt,
                after_publish=unittest.mock.Mock(
                    side_effect=AV1ValidationDerivationError(
                        "repository drift"
                    )
                ),
                published_before=self.plan.authorization.valid_until,
            )
        self.assertFalse(attempt_path.exists())

    def test_attempt_acceptance_marker_post_publish_identity_failure_removes_marker(
            self,
    ) -> None:
        attempts_directory = (
            self.runtime_config.paths.web_state_dir / "drifted-attempt-marker"
        )
        attempt = self._review_pending_attempt()
        after_publish = unittest.mock.Mock(side_effect=(
            None,
            AV1ValidationDerivationError("repository drift"),
        ))

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "repository drift",
        ):
            write_av1_validation_derivation_attempt(
                attempts_directory,
                attempt,
                after_publish=after_publish,
                published_before=self.plan.authorization.valid_until,
            )

        self.assertEqual(after_publish.call_count, 2)
        self.assertTrue(
            (attempts_directory / f"{attempt.assignment_id}.json").is_file()
        )
        self.assertFalse(
            (
                attempts_directory
                / ".publication"
                / f"{attempt.assignment_id}.accepted"
            ).exists()
        )
        self.assertEqual(
            load_av1_validation_derivation_attempt_publication_state(
                attempts_directory,
                attempt,
                published_before=self.plan.authorization.valid_until,
                require_durable=True,
            ).disposition,
            "unsealed",
        )

    def test_existing_accepted_attempt_rechecks_post_publish_identity(self) -> None:
        attempts_directory = (
            self.runtime_config.paths.web_state_dir / "existing-accepted-attempt"
        )
        attempt = self._review_pending_attempt()
        write_av1_validation_derivation_attempt(
            attempts_directory,
            attempt,
            published_before=self.plan.authorization.valid_until,
        )
        after_publish = unittest.mock.Mock(
            side_effect=AV1ValidationDerivationError("repository drift")
        )
        before_publish = unittest.mock.Mock(
            side_effect=AssertionError("existing attempt must not republish")
        )

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "repository drift",
        ):
            write_av1_validation_derivation_attempt(
                attempts_directory,
                attempt,
                before_publish=before_publish,
                after_publish=after_publish,
                published_before=self.plan.authorization.valid_until,
            )

        before_publish.assert_not_called()
        after_publish.assert_called_once_with()

    def test_existing_non_review_attempt_rechecks_post_publish_identity(self) -> None:
        attempts_directory = (
            self.runtime_config.paths.web_state_dir / "existing-failed-attempt"
        )
        attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=self.plan.assignments[0].assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="failed",
            reason_code="runtime_failure",
        )
        write_av1_validation_derivation_attempt(attempts_directory, attempt)
        after_publish = unittest.mock.Mock(
            side_effect=AV1ValidationDerivationError("repository drift")
        )
        before_publish = unittest.mock.Mock(
            side_effect=AssertionError("existing attempt must not republish")
        )

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "repository drift",
        ):
            write_av1_validation_derivation_attempt(
                attempts_directory,
                attempt,
                before_publish=before_publish,
                after_publish=after_publish,
            )

        before_publish.assert_not_called()
        after_publish.assert_called_once_with()

    def test_runtime_failure_reason_codes_are_immutable_terminal_evidence(self) -> None:
        assignment = self.plan.assignments[0]
        attempt_ids: set[str] = set()
        terminal_ids: set[str] = set()

        for reason_code in sorted(
            AV1_VALIDATION_DERIVATION_RUNTIME_FAILURE_REASON_CODES
        ):
            with self.subTest(reason_code=reason_code):
                attempt = build_av1_validation_derivation_attempt(
                    plan=self.plan,
                    partition=self.partition,
                    assignment_id=assignment.assignment_id,
                    started_at="2026-07-28T01:00:00Z",
                    completed_at="2026-07-28T01:05:00Z",
                    status="failed",
                    reason_code=reason_code,
                )
                terminal = build_av1_validation_derivation_terminal_record(
                    plan=self.plan,
                    partition=self.partition,
                    attempt=attempt,
                )

                self.assertEqual(attempt.reason_code, reason_code)
                self.assertEqual(terminal.reason_code, reason_code)
                self.assertEqual(terminal.attempt_id, attempt.attempt_id)
                self.assertEqual(
                    terminal.attempt_payload_sha256,
                    attempt.payload_sha256,
                )
                attempt_ids.add(attempt.attempt_id)
                terminal_ids.add(terminal.record_id)

        self.assertEqual(
            len(attempt_ids),
            len(AV1_VALIDATION_DERIVATION_RUNTIME_FAILURE_REASON_CODES),
        )
        self.assertEqual(
            len(terminal_ids),
            len(AV1_VALIDATION_DERIVATION_RUNTIME_FAILURE_REASON_CODES),
        )

    def test_legacy_runtime_failure_artifacts_remain_byte_exact_v2(self) -> None:
        assignment = self.plan.assignments[0]
        attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=assignment.assignment_id,
            started_at="2026-07-28T01:00:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="failed",
            reason_code="runtime_failure",
        )
        terminal = build_av1_validation_derivation_terminal_record(
            plan=self.plan,
            partition=self.partition,
            attempt=attempt,
        )
        self.assertEqual(
            set(attempt.to_payload()),
            {
                "attempt_id",
                "schema",
                "schema_version",
                "contract_version",
                "plan_id",
                "authorization_id",
                "assignment_id",
                "cell_plan_id",
                "ordinal",
                "started_at",
                "completed_at",
                "status",
                "reason_code",
                "calibration_payload",
                "calibration_payload_sha256",
                "payload_sha256",
            },
        )
        self.assertEqual(attempt.to_payload()["schema_version"], 2)
        self.assertEqual(attempt.to_payload()["contract_version"], "av1vdw2")
        self.assertEqual(
            set(terminal.to_payload()),
            {
                "record_id",
                "schema",
                "schema_version",
                "contract_version",
                "plan_id",
                "authorization_id",
                "attempt_id",
                "attempt_payload_sha256",
                "assignment_id",
                "cell_plan_id",
                "ordinal",
                "started_at",
                "completed_at",
                "status",
                "reason_code",
                "observation",
                "payload_sha256",
            },
        )
        self.assertEqual(terminal.to_payload()["schema_version"], 2)
        self.assertEqual(terminal.to_payload()["contract_version"], "av1vdw2")

        attempts_directory = self.runtime_artifact_root / "legacy-attempts"
        terminal_directory = self.runtime_artifact_root / "legacy-terminals"
        write_av1_validation_derivation_attempt(attempts_directory, attempt)
        write_av1_validation_derivation_terminal_record(
            terminal_directory,
            terminal,
        )
        self.assertEqual(
            (attempts_directory / f"{assignment.assignment_id}.json").read_bytes(),
            canonical_json_bytes(attempt.to_payload()),
        )
        self.assertEqual(
            (terminal_directory / f"{assignment.assignment_id}.json").read_bytes(),
            canonical_json_bytes(terminal.to_payload()),
        )
        self.assertEqual(
            load_av1_validation_derivation_attempts(attempts_directory),
            (attempt,),
        )
        self.assertEqual(
            load_av1_validation_derivation_terminal_records(terminal_directory),
            (terminal,),
        )

    def test_authoritative_attempt_load_requires_parent_durability(self) -> None:
        attempts_directory = self.runtime_config.paths.web_state_dir / "durable-attempts"
        attempt = self._review_pending_attempt()
        write_av1_validation_derivation_attempt(
            attempts_directory,
            attempt,
            published_before=self.plan.authorization.valid_until,
        )
        with (
            patch(
                "mediaforce.tuning.av1_validation_derivation._fsync_owner_only_parent",
                side_effect=AV1ValidationDerivationError(
                    "attempt directory sync failed"
                ),
            ) as sync_parent,
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "attempt directory sync failed",
            ),
        ):
            load_av1_validation_derivation_attempts(
                attempts_directory,
                review_pending_published_before=(
                    self.plan.authorization.valid_until
                ),
                require_durable=True,
            )
        sync_parent.assert_called_once_with(
            attempts_directory / f"{attempt.assignment_id}.json",
            "derivation attempt",
        )

    def test_existing_unsealed_attempt_cannot_be_retroactively_accepted(self) -> None:
        attempts_directory = self.runtime_config.paths.web_state_dir / "unsealed-attempts"
        attempt = self._review_pending_attempt()
        _bind_owner_only_directory(
            attempts_directory,
            kind="attempts",
            binding_id=attempt.plan_id,
            binding_digest=attempt.authorization_id,
        )
        attempt_path = attempts_directory / f"{attempt.assignment_id}.json"
        _write_owner_only(
            attempt_path,
            canonical_json_bytes(attempt.to_payload()),
        )

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "existing review-pending attempt was not durably accepted",
        ):
            write_av1_validation_derivation_attempt(
                attempts_directory,
                attempt,
                published_before=self.plan.authorization.valid_until,
            )

        self.assertFalse(
            (
                attempts_directory
                / ".publication"
                / f"{attempt.assignment_id}.accepted"
            ).exists()
        )
        self.assertEqual(attempt_path.read_bytes(), canonical_json_bytes(attempt.to_payload()))

    def test_attempt_publication_directory_rejects_orphan_marker(self) -> None:
        attempts_directory = self.runtime_config.paths.web_state_dir / "orphan-marker-attempts"
        attempt = self._review_pending_attempt()
        write_av1_validation_derivation_attempt(
            attempts_directory,
            attempt,
            published_before=self.plan.authorization.valid_until,
        )
        _write_owner_only(
            attempts_directory / ".publication" / "orphan.accepted",
            b"{}",
        )

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "contains an orphan artifact",
        ):
            load_av1_validation_derivation_attempts(
                attempts_directory,
                review_pending_published_before=self.plan.authorization.valid_until,
            )

    def test_unsealed_review_pending_attempt_is_rejected_and_terminalized(self) -> None:
        attempts_directory = self.runtime_artifact_root / "attempts"
        records_directory = self.runtime_artifact_root / "terminal-records"
        attempt = self._review_pending_attempt()
        _bind_owner_only_directory(
            attempts_directory,
            kind="attempts",
            binding_id=attempt.plan_id,
            binding_digest=attempt.authorization_id,
        )
        _write_owner_only(
            attempts_directory / f"{attempt.assignment_id}.json",
            canonical_json_bytes(attempt.to_payload()),
        )

        self.assertTrue(_recover_interrupted_derivation_state(
            plan=self.plan,
            partition=self.partition,
            artifact_root=self.runtime_artifact_root,
            attempts_directory=attempts_directory,
            terminal_records_directory=records_directory,
            completed_at="2026-07-28T01:06:00Z",
        ))

        publication_state = load_av1_validation_derivation_attempt_publication_state(
            attempts_directory,
            attempt,
            published_before=self.plan.authorization.valid_until,
        )
        self.assertEqual(publication_state.disposition, "rejected")
        self.assertEqual(publication_state.reason_code, "safety_stop")
        records = load_av1_validation_derivation_terminal_records(records_directory)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "stopped")
        self.assertEqual(records[0].reason_code, "safety_stop")
        self.assertFalse(_recover_interrupted_derivation_state(
            plan=self.plan,
            partition=self.partition,
            artifact_root=self.runtime_artifact_root,
            attempts_directory=attempts_directory,
            terminal_records_directory=records_directory,
            completed_at="2026-07-28T01:06:00Z",
        ))

    def test_unbound_attempt_publication_directory_recovers_as_unsealed(self) -> None:
        attempts_directory = self.runtime_artifact_root / "attempts"
        records_directory = self.runtime_artifact_root / "terminal-records"
        attempt = self._review_pending_attempt()
        _bind_owner_only_directory(
            attempts_directory,
            kind="attempts",
            binding_id=attempt.plan_id,
            binding_digest=attempt.authorization_id,
        )
        _write_owner_only(
            attempts_directory / f"{attempt.assignment_id}.json",
            canonical_json_bytes(attempt.to_payload()),
        )
        publication_directory = attempts_directory / ".publication"
        publication_directory.mkdir(mode=0o700)
        stale_binding_temporary = (
            publication_directory / f"..binding.{'a' * 24}.tmp"
        )
        stale_binding_temporary.write_bytes(b"partial")
        stale_binding_temporary.chmod(0o400)

        self.assertTrue(_recover_interrupted_derivation_state(
            plan=self.plan,
            partition=self.partition,
            artifact_root=self.runtime_artifact_root,
            attempts_directory=attempts_directory,
            terminal_records_directory=records_directory,
            completed_at="2026-07-28T01:06:00Z",
        ))

        self.assertTrue((publication_directory / ".binding").is_file())
        state = load_av1_validation_derivation_attempt_publication_state(
            attempts_directory,
            attempt,
            published_before=self.plan.authorization.valid_until,
        )
        self.assertEqual(state.disposition, "rejected")

    def test_unbound_attempt_publication_directory_rejects_marker(self) -> None:
        attempts_directory = self.runtime_artifact_root / "attempts"
        attempt = self._review_pending_attempt()
        _bind_owner_only_directory(
            attempts_directory,
            kind="attempts",
            binding_id=attempt.plan_id,
            binding_digest=attempt.authorization_id,
        )
        _write_owner_only(
            attempts_directory / f"{attempt.assignment_id}.json",
            canonical_json_bytes(attempt.to_payload()),
        )
        publication_directory = attempts_directory / ".publication"
        publication_directory.mkdir(mode=0o700)
        marker = publication_directory / f"{attempt.assignment_id}.accepted"
        marker.write_bytes(b"{}")
        marker.chmod(0o400)

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "unbound attempt-publication directory contains an artifact",
        ):
            load_av1_validation_derivation_attempt_publication_state(
                attempts_directory,
                attempt,
                published_before=self.plan.authorization.valid_until,
            )

    def test_late_unsealed_attempt_terminalizes_as_authorization_expired(self) -> None:
        attempts_directory = self.runtime_artifact_root / "attempts"
        records_directory = self.runtime_artifact_root / "terminal-records"
        attempt = self._review_pending_attempt()
        _bind_owner_only_directory(
            attempts_directory,
            kind="attempts",
            binding_id=attempt.plan_id,
            binding_digest=attempt.authorization_id,
        )
        _write_owner_only(
            attempts_directory / f"{attempt.assignment_id}.json",
            canonical_json_bytes(attempt.to_payload()),
        )

        with patch(
            "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
            return_value=10 ** 30,
        ):
            self.assertTrue(_recover_interrupted_derivation_state(
                plan=self.plan,
                partition=self.partition,
                artifact_root=self.runtime_artifact_root,
                attempts_directory=attempts_directory,
                terminal_records_directory=records_directory,
                completed_at="2026-07-28T01:06:00Z",
            ))

        publication_state = load_av1_validation_derivation_attempt_publication_state(
            attempts_directory,
            attempt,
            published_before=self.plan.authorization.valid_until,
        )
        self.assertEqual(publication_state.disposition, "rejected")
        self.assertEqual(publication_state.reason_code, "authorization_expired")
        records = load_av1_validation_derivation_terminal_records(records_directory)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "failed")
        self.assertEqual(records[0].reason_code, "authorization_expired")

    def test_interrupted_plan_retry_runs_gate_inside_binding_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
            root.mkdir(mode=0o700)
            artifact_root = root / self.plan.partition_id
            artifact_root.mkdir(mode=0o700)
            _write_owner_only(
                artifact_root / "plan.json",
                canonical_json_bytes(self.plan.to_payload()),
            )
            events: list[str] = []
            real_sync = _fsync_owner_only_artifact
            real_rename = _rename_owner_only_exclusive

            def track_sync(descriptor: int) -> None:
                events.append("binding_fsync")
                real_sync(descriptor)

            def before_bind() -> None:
                events.append("gate")
                self.assertFalse((artifact_root / ".binding").exists())

            def track_rename(**kwargs: object) -> None:
                events.append(f"rename:{kwargs['destination_name']}")
                real_rename(**kwargs)

            with (
                patch(
                    "mediaforce.tuning.av1_validation_derivation._fsync_owner_only_artifact",
                    side_effect=track_sync,
                ),
                patch(
                    "mediaforce.tuning.av1_validation_derivation._rename_owner_only_exclusive",
                    side_effect=track_rename,
                ),
            ):
                write_av1_validation_derivation_plan(
                    artifact_root,
                    self.plan,
                    before_bind=before_bind,
                )

            self.assertEqual(
                events[-3:],
                ["binding_fsync", "gate", "rename:.binding"],
            )

    def test_interrupted_plan_retry_rolls_back_late_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
            root.mkdir(mode=0o700)
            artifact_root = root / self.plan.partition_id
            artifact_root.mkdir(mode=0o700)
            plan_path = artifact_root / "plan.json"
            _write_owner_only(
                plan_path,
                canonical_json_bytes(self.plan.to_payload()),
            )
            plan_inode = plan_path.stat().st_ino

            with (
                patch(
                    "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
                    side_effect=lambda info: (
                        info.st_ctime_ns
                        if info.st_ino == plan_inode
                        else 10**30
                    ),
                ),
                self.assertRaises(AV1ValidationDerivationPublicationDeadlineError),
            ):
                write_av1_validation_derivation_plan(
                    artifact_root,
                    self.plan,
                    before_bind=lambda: None,
                )

            self.assertFalse((artifact_root / ".binding").exists())

    def test_interrupted_plan_retry_rolls_back_drifted_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
            root.mkdir(mode=0o700)
            artifact_root = root / self.plan.partition_id
            artifact_root.mkdir(mode=0o700)
            _write_owner_only(
                artifact_root / "plan.json",
                canonical_json_bytes(self.plan.to_payload()),
            )
            after_publish = unittest.mock.Mock(
                side_effect=AV1ValidationDerivationError(
                    "repository drift"
                )
            )

            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "repository drift",
            ):
                write_av1_validation_derivation_plan(
                    artifact_root,
                    self.plan,
                    after_publish=after_publish,
                    before_bind=lambda: None,
                )

            after_publish.assert_called_once_with()
            self.assertFalse((artifact_root / ".binding").exists())

    def test_existing_directory_binding_rechecks_post_publish_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "bound-root"
            _bind_owner_only_directory(
                artifact_root,
                kind="artifact_root",
                binding_id="plan-id",
                binding_digest="sha256:" + "1" * 64,
            )
            after_publish = unittest.mock.Mock(
                side_effect=AV1ValidationDerivationError("repository drift")
            )
            before_publish = unittest.mock.Mock(
                side_effect=AssertionError("existing binding must not republish")
            )

            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "repository drift",
            ):
                _bind_owner_only_directory(
                    artifact_root,
                    kind="artifact_root",
                    binding_id="plan-id",
                    binding_digest="sha256:" + "1" * 64,
                    before_publish=before_publish,
                    after_publish=after_publish,
                )

            before_publish.assert_not_called()
            after_publish.assert_called_once_with()

    def test_existing_plan_conflicting_binding_skips_post_publish_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
            root.mkdir(mode=0o700)
            artifact_root = root / self.plan.partition_id
            artifact_root.mkdir(mode=0o700)
            _write_owner_only(
                artifact_root / "plan.json",
                canonical_json_bytes(self.plan.to_payload()),
            )
            _bind_owner_only_directory(
                artifact_root,
                kind="artifact_root",
                binding_id=self.plan.plan_id,
                binding_digest="sha256:" + "f" * 64,
            )
            after_publish = unittest.mock.Mock()
            before_bind = unittest.mock.Mock(
                side_effect=AssertionError("existing binding must not republish")
            )

            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "bound to another artifact set",
            ):
                write_av1_validation_derivation_plan(
                    artifact_root,
                    self.plan,
                    after_publish=after_publish,
                    before_bind=before_bind,
                )

            before_bind.assert_not_called()
            after_publish.assert_not_called()

    def test_existing_directory_binding_preserves_deadline_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "bound-root"
            _bind_owner_only_directory(
                artifact_root,
                kind="artifact_root",
                binding_id="plan-id",
                binding_digest="sha256:" + "2" * 64,
            )

            with (
                patch(
                    "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
                    return_value=10**30,
                ),
                self.assertRaises(
                    AV1ValidationDerivationPublicationDeadlineError
                ),
            ):
                _bind_owner_only_directory(
                    artifact_root,
                    kind="artifact_root",
                    binding_id="plan-id",
                    binding_digest="sha256:" + "2" * 64,
                    published_before=JUST_BEFORE_VALID_UNTIL,
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

    def test_observed_terminal_retry_rejects_post_deadline_inode(self) -> None:
        assignment = self.plan.assignments[0]
        attempt = self._review_pending_attempt()
        terminal = build_av1_validation_derivation_terminal_record(
            plan=self.plan,
            partition=self.partition,
            attempt=attempt,
            observation=_observation(
                assignment=assignment,
                source_identity=_source_identity(self.partition, assignment),
                crf=28.0,
                bitrate=1_000_000,
                verdict="acceptable",
            ),
        )
        terminal_intents_directory = self.runtime_artifact_root / "terminal-intents"
        terminal_records_directory = self.runtime_artifact_root / "terminal-records"
        ensure_av1_validation_derivation_terminal_intent(
            terminal_intents_directory,
            terminal,
            published_before=VALID_UNTIL,
        )
        ensure_av1_validation_derivation_terminal_record(
            terminal_records_directory,
            terminal,
            published_before=VALID_UNTIL,
        )

        for directory, ensure_terminal in (
            (
                terminal_intents_directory,
                ensure_av1_validation_derivation_terminal_intent,
            ),
            (
                terminal_records_directory,
                ensure_av1_validation_derivation_terminal_record,
            ),
        ):
            with self.subTest(directory=directory.name):
                before_publish = unittest.mock.Mock()
                with (
                    patch(
                        "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
                        return_value=10 ** 30,
                    ),
                    self.assertRaises(AV1ValidationDerivationPublicationDeadlineError),
                ):
                    ensure_terminal(
                        directory,
                        terminal,
                        before_publish=before_publish,
                        published_before=VALID_UNTIL,
                    )
                before_publish.assert_not_called()

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

    def test_structured_review_uses_no_tool_request_and_removes_request_file(self) -> None:
        proposal = self._candidate_proposal()
        request_path: Path | None = None
        request_payload: dict[str, object] | None = None
        trusted_runner = Path("/private/trusted-code")

        def bundle_for_claim(*, claim: object, **_kwargs: object) -> dict[str, object]:
            return _review_bundle_for_claim(claim)

        def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
            nonlocal request_path, request_payload
            self.assertEqual(command[:3], [str(trusted_runner), "llm", "request"])
            self.assertNotIn("exec", command)
            self.assertFalse(any(item in {"--tools", "--sandbox", "-a"} for item in command))
            self.assertIn("--format-strict", command)
            request_path = Path(command[command.index("--message-file") + 1])
            self.assertEqual(stat.S_IMODE(request_path.stat().st_mode), 0o400)
            request_payload = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(
                command[command.index("--developer") + 1],
                av1_validation_derivation_review_developer_text("architecture"),
            )
            response_schema = json.loads(command[command.index("--schema-json") + 1])
            for field in (
                "lane",
                "proposal_id",
                "proposal_payload_sha256",
                "repository_commit",
                "repository_tree",
                "review_claim_id",
                "review_claim_payload_sha256",
                "review_run_id",
            ):
                self.assertEqual(
                    response_schema["properties"][field],
                    {"const": request_payload[field]},
                )
            response = _structured_review_response_from_request(request_payload)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(response, separators=(",", ":")) + "\n",
                stderr="parent stderr must not become review evidence",
            )

        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "assert_av1_validation_derivation_execution_environment",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_authorized_review_runner_identity",
                return_value=(
                    trusted_runner,
                    self.plan.review_runner_canonical_path_sha256,
                    self.plan.review_runner_binary_sha256,
                ),
            ) as runner_identity,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_repository_review_identity",
                return_value=(REVIEW_REPOSITORY_COMMIT, REVIEW_REPOSITORY_TREE),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_code_llm_request_contract",
            ) as assert_contract,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_build_av1_validation_derivation_review_bundle",
                side_effect=bundle_for_claim,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_command",
                side_effect=fake_run,
            ) as run_command,
            patch.object(
                verify_av1_cold_start_preregistration.uuid,
                "uuid4",
                return_value="90000000-0000-0000-0000-000000000001",
            ) as uuid4,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_now_iso",
                return_value="2026-07-28T03:00:01Z",
            ) as now_iso,
        ):
            claim, evidence, decision, reviewed_at = (
                verify_av1_cold_start_preregistration._run_code_llm_review(
                    artifact_root=self.runtime_artifact_root,
                    plan=self.plan,
                    proposal=proposal,
                    lane="architecture",
                )
            )
            claim_path = (
                self.runtime_artifact_root
                / "review-claims"
                / proposal.proposal_id
                / "architecture.json"
            )
            claim_info = claim_path.stat(follow_symlinks=False)
            (
                retried_claim,
                retried_evidence,
                retried_decision,
                retried_reviewed_at,
            ) = (
                verify_av1_cold_start_preregistration._run_code_llm_review(
                    artifact_root=self.runtime_artifact_root,
                    plan=self.plan,
                    proposal=proposal,
                    lane="architecture",
                    claim=claim,
                )
            )
        self.assertEqual(decision, "approved")
        self.assertEqual(retried_decision, "approved")
        self.assertEqual(retried_claim, claim)
        self.assertEqual(retried_evidence, evidence)
        self.assertEqual(retried_reviewed_at, reviewed_at)
        self.assertEqual(run_command.call_count, 1)
        self.assertEqual(runner_identity.call_count, 2)
        self.assertEqual(assert_contract.call_count, 1)
        self.assertEqual(uuid4.call_count, 1)
        self.assertEqual(now_iso.call_count, 2)
        retried_claim_info = claim_path.stat(follow_symlinks=False)
        self.assertEqual(
            (retried_claim_info.st_dev, retried_claim_info.st_ino),
            (claim_info.st_dev, claim_info.st_ino),
        )
        self.assertEqual(retried_claim_info.st_mtime_ns, claim_info.st_mtime_ns)
        assert request_path is not None
        self.assertFalse(request_path.exists())
        self.assertFalse(request_path.parent.exists())
        assert request_payload is not None
        evidence_payload = json.loads(evidence)
        self.assertEqual(
            evidence_payload["schema"],
            AV1_VALIDATION_DERIVATION_CHECKPOINTED_REVIEW_RUN_SCHEMA,
        )
        self.assertEqual(evidence_payload["request"], request_payload)
        self.assertEqual(evidence_payload["safe_bundle"], request_payload["safe_bundle"])
        self.assertEqual(evidence_payload["returncode"], 0)
        self.assertEqual(
            evidence_payload["stderr_sha256"],
            f"sha256:{hashlib.sha256(b'parent stderr must not become review evidence').hexdigest()}",
        )
        self.assertNotIn("stderr", evidence_payload)
        self.assertNotIn("transcript", evidence_payload)
        checkpoint = load_av1_validation_derivation_review_response_checkpoint(
            self.runtime_artifact_root,
            plan=self.plan,
            proposal=proposal,
            claim=claim,
        )
        self.assertIsNotNone(checkpoint)
        review = build_av1_validation_derivation_review_attestation(
            proposal=proposal,
            claim=claim,
            review_evidence_sha256=f"sha256:{hashlib.sha256(evidence).hexdigest()}",
            decision=decision,
            reviewed_at=reviewed_at,
        )
        validate_av1_validation_derivation_review_run_evidence(evidence, review=review)

    def test_review_response_checkpoint_is_strictly_predeadline_and_refsynced(
            self,
    ) -> None:
        proposal = self._candidate_proposal()
        claim = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            repository_commit=REVIEW_REPOSITORY_COMMIT,
            repository_tree=REVIEW_REPOSITORY_TREE,
            lane="architecture",
            review_run_id="90000000-0000-0000-0000-000000000004",
            review_runner_canonical_path_sha256=(
                self.plan.review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=self.plan.review_runner_binary_sha256,
            claimed_at="2026-07-28T03:00:01Z",
        )
        request_sha256 = f"sha256:{'a' * 64}"
        stderr_sha256 = f"sha256:{hashlib.sha256(b'').hexdigest()}"
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "outside authorization",
        ):
            write_av1_validation_derivation_review_response_checkpoint(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
                request_sha256=request_sha256,
                response="{}\n",
                stderr_sha256=stderr_sha256,
                completed_at=self.plan.authorization.valid_until,
            )
        write_av1_validation_derivation_review_response_checkpoint(
            self.runtime_artifact_root,
            plan=self.plan,
            proposal=proposal,
            claim=claim,
            request_sha256=request_sha256,
            response="{}\n",
            stderr_sha256=stderr_sha256,
            completed_at="2026-07-28T03:00:02Z",
        )
        checkpoint_directory = (
            self.runtime_artifact_root
            / "review-responses"
            / proposal.proposal_id
        )
        checkpoint_directory_identity = (
            checkpoint_directory.stat().st_dev,
            checkpoint_directory.stat().st_ino,
        )
        real_fsync = os.fsync
        parent_refsynced = False

        def track_fsync(descriptor: int) -> None:
            nonlocal parent_refsynced
            descriptor_info = os.fstat(descriptor)
            if (
                descriptor_info.st_dev,
                descriptor_info.st_ino,
            ) == checkpoint_directory_identity:
                parent_refsynced = True
            real_fsync(descriptor)

        with patch(
            "mediaforce.tuning.av1_validation_derivation.os.fsync",
            side_effect=track_fsync,
        ):
            checkpoint = load_av1_validation_derivation_review_response_checkpoint(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
            )
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint["returncode"], 0)
        self.assertTrue(parent_refsynced)

    def test_predeadline_review_checkpoint_seals_after_deadline_without_runner_probe(
            self,
    ) -> None:
        proposal = self._candidate_proposal()
        write_av1_validation_derivation_candidate_proposal(
            self.runtime_artifact_root,
            plan=self.plan,
            proposal=proposal,
        )
        claims: dict[str, AV1ValidationDerivationReviewClaim] = {}
        for lane, decision, run_id in (
            (
                "architecture",
                "approved",
                "90000000-0000-0000-0000-000000000005",
            ),
            (
                "adversarial",
                "rejected",
                "90000000-0000-0000-0000-000000000006",
            ),
        ):
            claim = build_av1_validation_derivation_review_claim(
                plan=self.plan,
                proposal=proposal,
                repository_commit=REVIEW_REPOSITORY_COMMIT,
                repository_tree=REVIEW_REPOSITORY_TREE,
                lane=lane,
                review_run_id=run_id,
                review_runner_canonical_path_sha256=(
                    self.plan.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.plan.review_runner_binary_sha256
                ),
                claimed_at="2026-07-28T03:00:01Z",
            )
            claims[lane] = claim
            write_av1_validation_derivation_review_claim(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
            )
            bundle = _review_bundle_for_claim(claim)
            request = build_av1_validation_derivation_review_request(
                proposal=proposal,
                claim=claim,
                safe_bundle=bundle,
            )
            response = _structured_review_response(
                proposal,
                claim,
                decision=decision,
            )
            write_av1_validation_derivation_review_response_checkpoint(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
                request_sha256=(
                    f"sha256:{hashlib.sha256(canonical_json_bytes(request)).hexdigest()}"
                ),
                response=json.dumps(response, separators=(",", ":")) + "\n",
                stderr_sha256=f"sha256:{hashlib.sha256(b'').hexdigest()}",
                completed_at="2026-07-28T03:00:02Z",
            )

        derivation_module = import_module(
            "mediaforce.tuning.av1_validation_derivation"
        )
        real_publication_check = (
            derivation_module._assert_owner_only_publication_before
        )

        def reject_late_uncheckpointed_envelope(
                path: Path,
                label: str,
                *,
                info: os.stat_result,
                published_before: str,
        ) -> None:
            if label in {
                "private derivation artifact",
                "derivation review envelope",
            }:
                raise AV1ValidationDerivationPublicationDeadlineError(
                    path,
                    label,
                )
            real_publication_check(
                path,
                label,
                info=info,
                published_before=published_before,
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
                side_effect=AssertionError("checkpoint recovery probed environment"),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_repository_review_identity",
                return_value=(REVIEW_REPOSITORY_COMMIT, REVIEW_REPOSITORY_TREE),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_build_av1_validation_derivation_review_bundle",
                side_effect=lambda *, claim, **_kwargs: _review_bundle_for_claim(
                    claim
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_authorized_review_runner_identity",
                side_effect=AssertionError("checkpoint recovery probed runner"),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_code_llm_request_contract",
                side_effect=AssertionError("checkpoint recovery probed contract"),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_command",
                side_effect=AssertionError("checkpoint recovery relaunched review"),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_now_iso",
                side_effect=AssertionError("checkpoint recovery changed review time"),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_print_partition_payload",
            ),
            patch.object(
                derivation_module,
                "_assert_owner_only_publication_before",
                side_effect=reject_late_uncheckpointed_envelope,
            ),
        ):
            for lane, expected_exit_code in (
                ("architecture", 0),
                ("adversarial", 2),
            ):
                with self.subTest(lane=lane):
                    args = SimpleNamespace(
                        action="record-derivation-review",
                        config=Path("unused.toml"),
                        plan=self.runtime_artifact_root / "plan.json",
                        cell_plan_id=proposal.cell_plan_id,
                        lane=lane,
                        json_output=True,
                    )
                    exit_code = (
                        verify_av1_cold_start_preregistration._run_derivation_action_body(
                            args,
                            locked_config=self.runtime_config,
                        )
                    )
                    self.assertEqual(exit_code, expected_exit_code)
                    envelope = load_av1_validation_derivation_review_envelope(
                        self.runtime_artifact_root,
                        plan=self.plan,
                        proposal=proposal,
                        claim=claims[lane],
                    )
                    evidence = json.loads(envelope.review_run_payload_json)
                    self.assertEqual(
                        evidence["schema"],
                        AV1_VALIDATION_DERIVATION_CHECKPOINTED_REVIEW_RUN_SCHEMA,
                    )
                    self.assertEqual(
                        envelope.review.reviewed_at,
                        "2026-07-28T03:00:02Z",
                    )

            architecture_checkpoint = (
                self.runtime_artifact_root
                / "review-responses"
                / proposal.proposal_id
                / "architecture.json"
            )
            original_checkpoint = architecture_checkpoint.read_bytes()
            original_checkpoint_mode = stat.S_IMODE(
                architecture_checkpoint.stat(follow_symlinks=False).st_mode
            )
            mismatched_checkpoint = json.loads(original_checkpoint)
            mismatched_checkpoint["request_sha256"] = f"sha256:{'f' * 64}"
            checkpoint_semantic_payload = dict(mismatched_checkpoint)
            checkpoint_semantic_payload.pop("payload_sha256")
            mismatched_checkpoint["payload_sha256"] = (
                _payload_sha256(checkpoint_semantic_payload)
            )
            try:
                architecture_checkpoint.chmod(0o600)
                architecture_checkpoint.write_bytes(
                    canonical_json_bytes(mismatched_checkpoint)
                )
                architecture_checkpoint.chmod(original_checkpoint_mode)
                with self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "checkpoint authority is unavailable",
                ):
                    load_av1_validation_derivation_review_envelope(
                        self.runtime_artifact_root,
                        plan=self.plan,
                        proposal=proposal,
                        claim=claims["architecture"],
                    )
            finally:
                architecture_checkpoint.chmod(0o600)
                architecture_checkpoint.write_bytes(original_checkpoint)
                architecture_checkpoint.chmod(original_checkpoint_mode)
            architecture_checkpoint.rename(
                architecture_checkpoint.with_suffix(".missing")
            )
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "checkpoint authority is unavailable",
            ):
                load_av1_validation_derivation_review_envelope(
                    self.runtime_artifact_root,
                    plan=self.plan,
                    proposal=proposal,
                    claim=claims["architecture"],
                )

    def test_interrupted_review_claim_terminalizes_without_relaunch(self) -> None:
        proposal = self._candidate_proposal()
        claim = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            repository_commit=REVIEW_REPOSITORY_COMMIT,
            repository_tree=REVIEW_REPOSITORY_TREE,
            lane="architecture",
            review_run_id="90000000-0000-0000-0000-000000000002",
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
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "assert_av1_validation_derivation_execution_environment",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_authorized_review_runner_identity",
            ) as runner_identity,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_repository_review_identity",
                return_value=(REVIEW_REPOSITORY_COMMIT, REVIEW_REPOSITORY_TREE),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_code_llm_request_contract",
            ) as assert_contract,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_build_av1_validation_derivation_review_bundle",
            ) as build_bundle,
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_command",
            ) as run_command,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_now_iso",
                return_value="2026-07-28T03:30:00Z",
            ),
        ):
            recovered_claim, evidence, decision, reviewed_at = (
                verify_av1_cold_start_preregistration._run_code_llm_review(
                    artifact_root=self.runtime_artifact_root,
                    plan=self.plan,
                    proposal=proposal,
                    lane="architecture",
                    claim=claim,
                )
            )
        self.assertEqual(recovered_claim, claim)
        self.assertEqual(decision, "rejected")
        self.assertEqual(reviewed_at, "2026-07-28T03:30:00Z")
        runner_identity.assert_not_called()
        assert_contract.assert_not_called()
        run_command.assert_not_called()
        build_bundle.assert_not_called()
        evidence_payload = json.loads(evidence)
        self.assertEqual(
            evidence_payload["schema"],
            AV1_VALIDATION_DERIVATION_REVIEW_RECOVERY_SCHEMA,
        )
        self.assertEqual(
            evidence_payload["reason_code"],
            "interrupted_before_durable_response",
        )
        review = build_av1_validation_derivation_review_attestation(
            proposal=proposal,
            claim=claim,
            review_evidence_sha256=f"sha256:{hashlib.sha256(evidence).hexdigest()}",
            decision=decision,
            reviewed_at=reviewed_at,
        )
        envelope = build_av1_validation_derivation_review_envelope(
            review=review,
            evidence=evidence,
        )
        self.assertEqual(envelope.review.decision, "rejected")
        claims = [claim]
        envelopes = [envelope]
        for index, lane in enumerate(
            AV1_VALIDATION_DERIVATION_REVIEW_LANES[1:],
            start=2,
        ):
            approved_claim = build_av1_validation_derivation_review_claim(
                plan=self.plan,
                proposal=proposal,
                repository_commit=REVIEW_REPOSITORY_COMMIT,
                repository_tree=REVIEW_REPOSITORY_TREE,
                lane=lane,
                review_run_id=f"91000000-0000-0000-0000-{index:012x}",
                review_runner_canonical_path_sha256=(
                    self.plan.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.plan.review_runner_binary_sha256
                ),
                claimed_at=f"2026-07-28T03:00:{index:02d}Z",
            )
            approved_evidence = _review_run_evidence(
                proposal=proposal,
                claim=approved_claim,
            )
            approved_review = build_av1_validation_derivation_review_attestation(
                proposal=proposal,
                claim=approved_claim,
                review_evidence_sha256=(
                    f"sha256:{hashlib.sha256(approved_evidence).hexdigest()}"
                ),
                decision="approved",
                reviewed_at=f"2026-07-28T03:{index:02d}:00Z",
            )
            claims.append(approved_claim)
            envelopes.append(build_av1_validation_derivation_review_envelope(
                review=approved_review,
                evidence=approved_evidence,
            ))
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "terminal rejection",
        ):
            _av1_validation_derivation_review_set_sha256(
                self.plan,
                claims,
                envelopes,
            )

    def test_invalid_durable_review_response_terminalizes_without_relaunch(
            self,
    ) -> None:
        proposal = self._candidate_proposal()
        trusted_runner = Path("/private/trusted-code")

        def bundle_for_claim(*, claim: object, **_kwargs: object) -> dict[str, object]:
            return _review_bundle_for_claim(claim)

        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "assert_av1_validation_derivation_execution_environment",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_authorized_review_runner_identity",
                return_value=(
                    trusted_runner,
                    self.plan.review_runner_canonical_path_sha256,
                    self.plan.review_runner_binary_sha256,
                ),
            ) as runner_identity,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_repository_review_identity",
                return_value=(REVIEW_REPOSITORY_COMMIT, REVIEW_REPOSITORY_TREE),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_code_llm_request_contract",
            ) as assert_contract,
            patch.object(
                verify_av1_cold_start_preregistration,
                "_build_av1_validation_derivation_review_bundle",
                side_effect=bundle_for_claim,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_command",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="{}\n",
                    stderr="",
                ),
            ) as run_command,
            patch.object(
                verify_av1_cold_start_preregistration.uuid,
                "uuid4",
                return_value="90000000-0000-0000-0000-000000000003",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_now_iso",
                side_effect=(
                    "2026-07-28T03:00:01Z",
                    "2026-07-28T03:00:02Z",
                    "2026-07-28T03:00:03Z",
                    "2026-07-28T03:00:04Z",
                ),
            ),
        ):
            claim, evidence, decision, reviewed_at = (
                verify_av1_cold_start_preregistration._run_code_llm_review(
                    artifact_root=self.runtime_artifact_root,
                    plan=self.plan,
                    proposal=proposal,
                    lane="architecture",
                )
            )
            _, retried_evidence, retried_decision, _ = (
                verify_av1_cold_start_preregistration._run_code_llm_review(
                    artifact_root=self.runtime_artifact_root,
                    plan=self.plan,
                    proposal=proposal,
                    lane="architecture",
                    claim=claim,
                )
            )
        self.assertEqual(decision, "rejected")
        self.assertEqual(retried_decision, "rejected")
        self.assertEqual(run_command.call_count, 1)
        self.assertEqual(runner_identity.call_count, 2)
        self.assertEqual(assert_contract.call_count, 1)
        for recovery_evidence in (evidence, retried_evidence):
            payload = json.loads(recovery_evidence)
            self.assertEqual(
                payload["schema"],
                AV1_VALIDATION_DERIVATION_REVIEW_RECOVERY_SCHEMA,
            )
            self.assertEqual(payload["reason_code"], "invalid_durable_response")
            self.assertEqual(payload["schema_version"], 2)
            self.assertIs(payload["checkpoint_completion_bound"], True)
        review = build_av1_validation_derivation_review_attestation(
            proposal=proposal,
            claim=claim,
            review_evidence_sha256=f"sha256:{hashlib.sha256(evidence).hexdigest()}",
            decision=decision,
            reviewed_at=reviewed_at,
        )
        modern_envelope = build_av1_validation_derivation_review_envelope(
            review=review,
            evidence=evidence,
        )
        for invalid_schema_version in (2.0, True):
            malformed_payload = json.loads(evidence)
            malformed_payload["schema_version"] = invalid_schema_version
            malformed_evidence = canonical_json_bytes(malformed_payload)
            malformed_review = build_av1_validation_derivation_review_attestation(
                proposal=proposal,
                claim=claim,
                review_evidence_sha256=(
                    f"sha256:{hashlib.sha256(malformed_evidence).hexdigest()}"
                ),
                decision="rejected",
                reviewed_at=reviewed_at,
            )
            with (
                self.subTest(schema_version=invalid_schema_version),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "schema version is invalid",
                ),
            ):
                build_av1_validation_derivation_review_envelope(
                    review=malformed_review,
                    evidence=malformed_evidence,
                )
        checkpoint = load_av1_validation_derivation_review_response_checkpoint(
            self.runtime_artifact_root,
            plan=self.plan,
            proposal=proposal,
            claim=claim,
        )
        assert checkpoint is not None
        checkpoint_digest = checkpoint["payload_sha256"]
        assert isinstance(checkpoint_digest, str)
        self.assertEqual(
            checkpoint["completed_at"],
            "2026-07-28T03:00:02Z",
        )

        def legacy_envelope_at(
                recovered_at: str,
        ) -> AV1ValidationDerivationReviewEnvelope:
            recovery_payload = json.loads(
                build_av1_validation_derivation_review_recovery_evidence(
                    proposal=proposal,
                    claim=claim,
                    reason_code="invalid_durable_response",
                    recovered_at=recovered_at,
                    response_checkpoint_payload_sha256=checkpoint_digest,
                )
            )
            recovery_payload["schema_version"] = 1
            recovery_payload.pop("checkpoint_completion_bound")
            recovery_evidence = canonical_json_bytes(recovery_payload)
            recovery_review = build_av1_validation_derivation_review_attestation(
                proposal=proposal,
                claim=claim,
                review_evidence_sha256=(
                    f"sha256:{hashlib.sha256(recovery_evidence).hexdigest()}"
                ),
                decision="rejected",
                reviewed_at=recovered_at,
            )
            return build_av1_validation_derivation_review_envelope(
                review=recovery_review,
                evidence=recovery_evidence,
            )

        derivation_module = import_module(
            "mediaforce.tuning.av1_validation_derivation"
        )
        self.assertTrue(
            derivation_module._assert_av1_validation_derivation_review_checkpoint_authority(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
                envelope=modern_envelope,
            )
        )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "predates its checkpoint",
        ):
            write_av1_validation_derivation_review_envelope(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
                envelope=legacy_envelope_at(claim.claimed_at),
            )
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "legacy invalid-response recovery is outside authorization",
        ):
            write_av1_validation_derivation_review_envelope(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
                envelope=legacy_envelope_at(AFTER_VALID_UNTIL),
            )
        equal_legacy_envelope = legacy_envelope_at(
            str(checkpoint["completed_at"])
        )
        self.assertFalse(
            derivation_module._assert_av1_validation_derivation_review_checkpoint_authority(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
                envelope=equal_legacy_envelope,
            )
        )
        real_publication_check = (
            derivation_module._assert_owner_only_publication_before
        )

        def reject_legacy_envelope_after_deadline(
                path: Path,
                label: str,
                *,
                info: os.stat_result,
                published_before: str,
        ) -> None:
            if label in {
                "private derivation artifact",
                "derivation review envelope",
            }:
                raise AV1ValidationDerivationPublicationDeadlineError(path, label)
            real_publication_check(
                path,
                label,
                info=info,
                published_before=published_before,
            )

        with (
            patch.object(
                derivation_module,
                "_assert_owner_only_publication_before",
                side_effect=reject_legacy_envelope_after_deadline,
            ),
            self.assertRaises(AV1ValidationDerivationPublicationDeadlineError),
        ):
            write_av1_validation_derivation_review_envelope(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
                envelope=equal_legacy_envelope,
            )

        legacy_envelope = legacy_envelope_at("2026-07-28T03:00:04Z")
        write_av1_validation_derivation_review_envelope(
            self.runtime_artifact_root,
            plan=self.plan,
            proposal=proposal,
            claim=claim,
            envelope=legacy_envelope,
        )
        self.assertEqual(
            load_av1_validation_derivation_review_envelope(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
            ),
            legacy_envelope,
        )

        with (
            patch.object(
                derivation_module,
                "_assert_owner_only_publication_before",
                side_effect=reject_legacy_envelope_after_deadline,
            ),
            self.assertRaises(AV1ValidationDerivationPublicationDeadlineError),
        ):
            load_av1_validation_derivation_review_envelope(
                self.runtime_artifact_root,
                plan=self.plan,
                proposal=proposal,
                claim=claim,
            )

    def test_code_llm_request_contract_and_environment_are_allowlisted(self) -> None:
        runner = Path("/private/trusted-code")
        help_output = " ".join((
            "--developer",
            "--message-file",
            "--format-type",
            "--format-name",
            "--format-strict",
            "--schema-json",
        ))
        with patch.object(
            verify_av1_cold_start_preregistration,
            "run_command",
            return_value=SimpleNamespace(
                returncode=0,
                stdout=help_output,
                stderr="",
            ),
        ) as run_help:
            verify_av1_cold_start_preregistration._assert_code_llm_request_contract(
                runner,
                process_controller=ManagedProcessController(),
            )
        self.assertEqual(
            run_help.call_args.args[0],
            [str(runner), "llm", "request", "--help"],
        )
        environment = run_help.call_args.kwargs["env"]
        self.assertEqual(environment["PWD"], str(Path("/tmp").resolve()))
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("OPENAI_BASE_URL", environment)

        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_command",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=" ".join((
                        "--developer",
                        "--message-file",
                        "--format-name",
                        "--format-strict",
                        "--schema-json",
                    )),
                    stderr="",
                ),
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "contract is incompatible",
            ),
        ):
            verify_av1_cold_start_preregistration._assert_code_llm_request_contract(
                runner,
                process_controller=ManagedProcessController(),
            )

    def test_review_git_environment_disables_replacements_locks_and_unsafe_config(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            _run_test_git(repository, "init", "--quiet")
            environment = verify_av1_cold_start_preregistration._review_git_environment(
                repository_root=repository,
            )
        self.assertEqual(environment["GIT_ATTR_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(
            environment["GIT_DIR"],
            str((repository / ".git").resolve()),
        )
        self.assertEqual(
            environment["GIT_COMMON_DIR"],
            str((repository / ".git").resolve()),
        )
        self.assertEqual(environment["GIT_WORK_TREE"], str(repository.resolve()))
        self.assertEqual(
            verify_av1_cold_start_preregistration._review_git_command("cat-file", "blob", "a" * 40),
            [
                verify_av1_cold_start_preregistration
                ._canonical_system_git_binary(sys.platform),
                "-c",
                "core.attributesFile=/dev/null",
                "-c",
                "core.excludesFile=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "cat-file",
                "blob",
                "a" * 40,
            ],
        )

    def test_review_git_identity_rejects_git_directory_replacement_during_probe(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            _run_test_git(repository, "init", "--quiet")
            original_git_directory = repository / ".git-original"
            replaced = False

            def replace_git_directory(
                    *_args: object,
                    **_kwargs: object,
            ) -> SimpleNamespace:
                nonlocal replaced
                if not replaced:
                    (repository / ".git").rename(original_git_directory)
                    (repository / ".git").mkdir()
                    replaced = True
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{'a' * 40}\n{'b' * 40}\n",
                    stderr="",
                )

            with (
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "run_command",
                    side_effect=replace_git_directory,
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "Git authority changed",
                ),
            ):
                verify_av1_cold_start_preregistration._repository_review_identity(
                    process_controller=ManagedProcessController(),
                    repository_root=repository,
                )

            self.assertTrue(replaced)

    def test_review_git_identity_rejects_transient_git_directory_replacement(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            _run_test_git(repository, "init", "--quiet")
            original_git_directory = repository / ".git-original"
            replaced = False

            def replace_and_restore_git_directory(
                    *_args: object,
                    **_kwargs: object,
            ) -> SimpleNamespace:
                nonlocal replaced
                (repository / ".git").rename(original_git_directory)
                (repository / ".git").mkdir()
                (repository / ".git").rmdir()
                original_git_directory.rename(repository / ".git")
                replaced = True
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{'a' * 40}\n{'b' * 40}\n",
                    stderr="",
                )

            with (
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "run_command",
                    side_effect=replace_and_restore_git_directory,
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "Git authority changed",
                ),
            ):
                verify_av1_cold_start_preregistration._repository_review_identity(
                    process_controller=ManagedProcessController(),
                    repository_root=repository,
                )

            self.assertTrue(replaced)

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "requires kqueue or inotify",
    )
    def test_review_git_identity_rejects_in_place_loose_ref_rewrite_and_restore(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            _run_test_git(repository, "init", "--quiet")
            (repository / "tracked.txt").write_text(
                "authoritative\n",
                encoding="utf-8",
            )
            _run_test_git(repository, "add", "tracked.txt")
            _run_test_git(repository, "commit", "--quiet", "-m", "loose ref")
            branch = _run_test_git(repository, "symbolic-ref", "--short", "HEAD")
            loose_ref = repository / ".git" / "refs" / "heads" / branch
            original = loose_ref.read_bytes()
            original_identity = (loose_ref.stat().st_dev, loose_ref.stat().st_ino)
            rewritten = False
            real_run_command = verify_av1_cold_start_preregistration.run_command

            def rewrite_and_restore_loose_ref(
                    *args: object,
                    **kwargs: object,
            ) -> object:
                nonlocal rewritten
                completed = real_run_command(*args, **kwargs)
                if not rewritten:
                    replacement = b"0" * len(original.rstrip(b"\n")) + b"\n"
                    with loose_ref.open("r+b", buffering=0) as handle:
                        handle.seek(0)
                        handle.write(replacement)
                        handle.truncate()
                        os.fsync(handle.fileno())
                        handle.seek(0)
                        handle.write(original)
                        handle.truncate()
                        os.fsync(handle.fileno())
                    rewritten = True
                return completed

            with (
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "run_command",
                    side_effect=rewrite_and_restore_loose_ref,
                ),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "Git authority changed",
                ),
            ):
                verify_av1_cold_start_preregistration._repository_review_identity(
                    process_controller=ManagedProcessController(),
                    repository_root=repository,
                )

            self.assertTrue(rewritten)
            self.assertEqual(loose_ref.read_bytes(), original)
            self.assertEqual(
                (loose_ref.stat().st_dev, loose_ref.stat().st_ino),
                original_identity,
            )

    def test_review_git_probes_ignore_replace_refs_for_identity_and_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            _run_test_git(repository, "init", "--quiet")
            (repository / "allowed.txt").write_text(
                "authoritative review text\n",
                encoding="utf-8",
            )
            _run_test_git(repository, "add", "allowed.txt")
            _run_test_git(repository, "commit", "--quiet", "-m", "authoritative")
            authoritative_commit = _run_test_git(repository, "rev-parse", "HEAD")
            authoritative_tree = _run_test_git(repository, "rev-parse", "HEAD^{tree}")
            authoritative_blob = _run_test_git(
                repository,
                "rev-parse",
                "HEAD:allowed.txt",
            )
            (repository / "allowed.txt").write_text(
                "replacement review text\n",
                encoding="utf-8",
            )
            _run_test_git(repository, "add", "allowed.txt")
            _run_test_git(repository, "commit", "--quiet", "-m", "replacement")
            replacement_commit = _run_test_git(repository, "rev-parse", "HEAD")
            replacement_blob = _run_test_git(
                repository,
                "rev-parse",
                "HEAD:allowed.txt",
            )
            _run_test_git(repository, "checkout", "--quiet", "--detach", authoritative_commit)
            claim = SimpleNamespace(
                lane="architecture",
                repository_commit=authoritative_commit,
                repository_tree=authoritative_tree,
            )
            with patch.object(
                verify_av1_cold_start_preregistration,
                "AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_ALLOWLIST",
                {"architecture": ("allowed.txt",)},
            ):
                expected_identity = (
                    verify_av1_cold_start_preregistration._repository_review_identity(
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    )
                )
                expected_bundle = (
                    verify_av1_cold_start_preregistration._build_av1_validation_derivation_review_bundle(
                        claim=claim,
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    )
                )
                _run_test_git(
                    repository,
                    "replace",
                    authoritative_commit,
                    replacement_commit,
                )
                _run_test_git(
                    repository,
                    "replace",
                    authoritative_blob,
                    replacement_blob,
                )
                self.assertEqual(
                    verify_av1_cold_start_preregistration._repository_review_identity(
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    ),
                    expected_identity,
                )
                self.assertEqual(
                    verify_av1_cold_start_preregistration._build_av1_validation_derivation_review_bundle(
                        claim=claim,
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    ),
                    expected_bundle,
                )
            self.assertEqual(expected_identity, (authoritative_commit, authoritative_tree))
            self.assertEqual(expected_bundle["files"][0]["blob_id"], authoritative_blob)
            self.assertEqual(expected_bundle["files"][0]["text"], "authoritative review text\n")

    def test_review_git_identity_rejects_index_trust_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            _run_test_git(repository, "init", "--quiet")
            tracked_path = repository / "tracked.txt"
            tracked_path.write_text("authoritative\n", encoding="utf-8")
            _run_test_git(repository, "add", "tracked.txt")
            _run_test_git(repository, "commit", "--quiet", "-m", "authoritative")

            for flag in ("--assume-unchanged", "--skip-worktree"):
                with self.subTest(flag=flag):
                    _run_test_git(repository, "update-index", flag, "tracked.txt")
                    tracked_path.write_text("unreviewed\n", encoding="utf-8")
                    with self.assertRaisesRegex(
                        AV1ValidationDerivationError,
                        "unsafe index state",
                    ):
                        verify_av1_cold_start_preregistration._repository_review_identity(
                            process_controller=ManagedProcessController(),
                            repository_root=repository,
                        )
                    _run_test_git(
                        repository,
                        "update-index",
                        "--no-skip-worktree",
                        "tracked.txt",
                    )
                    _run_test_git(
                        repository,
                        "update-index",
                        "--no-assume-unchanged",
                        "tracked.txt",
                    )
                    _run_test_git(repository, "checkout", "--", "tracked.txt")

    def test_review_git_identity_rejects_ignored_implementation_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            (repository / "mediaforce").mkdir(parents=True)
            _run_test_git(repository, "init", "--quiet")
            (repository / ".gitignore").write_text(
                "*.pyc\n__pycache__/\n",
                encoding="utf-8",
            )
            (repository / "mediaforce" / "module.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            _run_test_git(repository, "add", ".gitignore", "mediaforce/module.py")
            _run_test_git(repository, "commit", "--quiet", "-m", "authoritative")
            (repository / "mediaforce" / "module.pyc").write_bytes(b"ignored bytecode")

            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "ignored implementation artifacts",
            ):
                verify_av1_cold_start_preregistration._repository_review_identity(
                    process_controller=ManagedProcessController(),
                    repository_root=repository,
                )

    def test_review_git_probes_reject_dirty_state_despite_local_diff_drivers(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            alternate_worktree = Path(directory) / "alternate-worktree"
            repository.mkdir()
            alternate_worktree.mkdir()
            _run_test_git(repository, "init", "--quiet")
            (repository / ".gitattributes").write_text(
                "tracked.txt diff=review-mask\n",
                encoding="utf-8",
            )
            (repository / "tracked.txt").write_text(
                "authoritative tracked text\n",
                encoding="utf-8",
            )
            _run_test_git(repository, "add", ".gitattributes", "tracked.txt")
            _run_test_git(repository, "commit", "--quiet", "-m", "authoritative")
            commit = _run_test_git(repository, "rev-parse", "HEAD")
            tree = _run_test_git(repository, "rev-parse", "HEAD^{tree}")
            claim = SimpleNamespace(
                lane="architecture",
                repository_commit=commit,
                repository_tree=tree,
            )
            external_diff = Path(directory) / "external-diff.sh"
            external_diff.write_text(
                "#!/bin/sh\n: > \"${0}.called\"\nexit 0\n",
                encoding="utf-8",
            )
            external_diff.chmod(0o700)
            textconv = Path(directory) / "textconv.sh"
            textconv.write_text(
                "#!/bin/sh\n: > \"${0}.called\"\nprintf 'normalized\\n'\n",
                encoding="utf-8",
            )
            textconv.chmod(0o700)
            configured_attributes = Path(directory) / "configured-attributes"
            configured_attributes.write_text(
                "tracked.txt diff=review-mask\n",
                encoding="utf-8",
            )
            (alternate_worktree / "tracked.txt").write_text(
                "alternate worktree text\n",
                encoding="utf-8",
            )
            _run_test_git(
                repository,
                "config",
                "--local",
                "core.attributesFile",
                str(configured_attributes),
            )
            _run_test_git(
                repository,
                "config",
                "--local",
                "core.worktree",
                str(alternate_worktree),
            )
            _run_test_git(
                repository,
                "config",
                "--local",
                "diff.external",
                str(external_diff),
            )
            _run_test_git(
                repository,
                "config",
                "--local",
                "diff.review-mask.textconv",
                str(textconv),
            )
            with patch.object(
                verify_av1_cold_start_preregistration,
                "AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_ALLOWLIST",
                {"architecture": ("tracked.txt",)},
            ):
                self.assertEqual(
                    verify_av1_cold_start_preregistration._repository_review_identity(
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    ),
                    (commit, tree),
                )
                baseline_bundle = (
                    verify_av1_cold_start_preregistration._build_av1_validation_derivation_review_bundle(
                        claim=claim,
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    )
                )
                (repository / "untracked.txt").write_text(
                    "untracked worktree text\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "review repository is not clean",
                ):
                    verify_av1_cold_start_preregistration._repository_review_identity(
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    )
                self.assertEqual(
                    verify_av1_cold_start_preregistration._build_av1_validation_derivation_review_bundle(
                        claim=claim,
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    ),
                    baseline_bundle,
                )
                (repository / "untracked.txt").unlink()
                (repository / "tracked.txt").write_text(
                    "dirty tracked text\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "uncommitted tracked changes",
                ):
                    verify_av1_cold_start_preregistration._repository_review_identity(
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    )
            self.assertFalse(external_diff.with_name(f"{external_diff.name}.called").exists())
            self.assertFalse(textconv.with_name(f"{textconv.name}.called").exists())

    def test_review_git_raw_bytes_do_not_execute_clean_or_process_filters(self) -> None:
        for filter_kind in ("clean", "process"):
            with self.subTest(filter_kind=filter_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository = root / "repository"
                repository.mkdir()
                _run_test_git(repository, "init", "--quiet")
                tracked_path = repository / "tracked.txt"
                tracked_path.write_text("authoritative\n", encoding="utf-8")
                _run_test_git(repository, "add", "tracked.txt")
                _run_test_git(repository, "commit", "--quiet", "-m", "authoritative")
                sentinel = root / f"runtime-{filter_kind}-executed"
                filter_command = root / f"runtime-{filter_kind}.sh"
                filter_command.write_text(
                    "#!/bin/sh\n"
                    f": > {str(sentinel)!r}\n"
                    + ("cat\n" if filter_kind == "clean" else "exit 1\n"),
                    encoding="utf-8",
                )
                filter_command.chmod(0o700)
                info_directory = repository / ".git" / "info"
                info_directory.mkdir(exist_ok=True)
                (info_directory / "attributes").write_text(
                    "tracked.txt filter=hostile\n",
                    encoding="utf-8",
                )
                _run_test_git(
                    repository,
                    "config",
                    "--local",
                    f"filter.hostile.{filter_kind}",
                    str(filter_command),
                )
                tracked_path.write_text("dirty\n", encoding="utf-8")

                with self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "uncommitted tracked changes",
                ):
                    verify_av1_cold_start_preregistration._repository_review_identity(
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    )

                self.assertFalse(sentinel.exists())

    def test_review_git_raw_bytes_support_sha256_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            git_binary = (
                verify_av1_cold_start_preregistration
                ._canonical_system_git_binary(sys.platform)
            )
            self.assertTrue(
                verify_av1_cold_start_preregistration
                ._system_git_binary_is_trusted(git_binary)
            )
            completed = subprocess.run(
                [
                    git_binary,
                    "init",
                    "--quiet",
                    "--object-format=sha256",
                    str(repository),
                ],
                capture_output=True,
                text=True,
                check=False,
                env={
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "HOME": directory,
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin",
                },
            )
            if completed.returncode != 0:
                self.skipTest("installed Git does not support SHA-256 repositories")
            tracked_path = repository / "tracked.txt"
            tracked_path.write_text("authoritative\n", encoding="utf-8")
            _run_test_git(repository, "add", "tracked.txt")
            _run_test_git(repository, "commit", "--quiet", "-m", "sha256")

            commit, tree = (
                verify_av1_cold_start_preregistration._repository_review_identity(
                    process_controller=ManagedProcessController(),
                    repository_root=repository,
                )
            )
            self.assertEqual(len(commit), 64)
            self.assertEqual(len(tree), 64)
            tracked_path.write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "uncommitted tracked changes",
            ):
                verify_av1_cold_start_preregistration._repository_review_identity(
                    process_controller=ManagedProcessController(),
                    repository_root=repository,
                )

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux path bytes")
    def test_review_git_round_trips_non_utf8_tracked_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            _run_test_git(repository, "init", "--quiet")
            raw_name = b"tracked-\xff.txt"
            raw_path = os.path.join(os.fsencode(repository), raw_name)
            descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, b"authoritative\n")
            finally:
                os.close(descriptor)
            decoded_name = os.fsdecode(raw_name)
            _run_test_git(repository, "add", "--", decoded_name)
            _run_test_git(repository, "commit", "--quiet", "-m", "path bytes")

            identity = verify_av1_cold_start_preregistration._repository_review_identity(
                process_controller=ManagedProcessController(),
                repository_root=repository,
            )
            self.assertEqual(len(identity), 2)
            descriptor = os.open(raw_path, os.O_WRONLY | os.O_TRUNC)
            try:
                os.write(descriptor, b"dirty\n")
            finally:
                os.close(descriptor)
            with self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "uncommitted tracked changes",
            ):
                verify_av1_cold_start_preregistration._repository_review_identity(
                    process_controller=ManagedProcessController(),
                    repository_root=repository,
                )

    def test_review_bundle_allowlists_fit_current_source_bounds(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        for lane, paths in AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_ALLOWLIST.items():
            sizes = []
            for path in paths:
                size = (repository_root / path).stat().st_size
                self.assertLessEqual(
                    size,
                    AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BLOB_BYTES,
                    path,
                )
                sizes.append(size)
            self.assertLessEqual(
                sum(sizes),
                AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BUNDLE_BYTES,
                lane,
            )

    def test_review_bundle_uses_tracked_utf8_allowlist_and_rejects_unsafe_blobs(
            self,
    ) -> None:
        cases = (
            ("valid", "allowed.txt", b"tracked review text\n", False, None),
            ("symlink", "allowed-link", b"target\n", True, "allowlisted blob"),
            (
                "oversize",
                "oversized.txt",
                b"x" * (AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BLOB_BYTES + 1),
                False,
                "oversized",
            ),
            ("utf8", "invalid.txt", b"\xff", False, "not UTF-8"),
        )
        for label, path, payload, symlink, error in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                repository = Path(directory) / "repository"
                repository.mkdir()
                _run_test_git(repository, "init", "--quiet")
                if symlink:
                    (repository / "target").write_bytes(payload)
                    (repository / path).symlink_to("target")
                    _run_test_git(repository, "add", "target", path)
                else:
                    (repository / path).write_bytes(payload)
                    _run_test_git(repository, "add", path)
                _run_test_git(repository, "commit", "--quiet", "-m", label)
                claim = SimpleNamespace(
                    lane="architecture",
                    repository_commit=_run_test_git(repository, "rev-parse", "HEAD"),
                    repository_tree=_run_test_git(repository, "rev-parse", "HEAD^{tree}"),
                )
                with patch.object(
                    verify_av1_cold_start_preregistration,
                    "AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_ALLOWLIST",
                    {"architecture": (path,)},
                ):
                    if error is not None:
                        with self.assertRaisesRegex(AV1ValidationDerivationError, error):
                            verify_av1_cold_start_preregistration._build_av1_validation_derivation_review_bundle(
                                claim=claim,
                                process_controller=ManagedProcessController(),
                                repository_root=repository,
                            )
                        continue
                    bundle = verify_av1_cold_start_preregistration._build_av1_validation_derivation_review_bundle(
                        claim=claim,
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    )
                    (repository / "untracked-secret.txt").write_text(
                        "never include me", encoding="utf-8"
                    )
                    repeated = verify_av1_cold_start_preregistration._build_av1_validation_derivation_review_bundle(
                        claim=claim,
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    )
                self.assertEqual(bundle, repeated)
                self.assertNotIn(
                    "untracked-secret",
                    canonical_json_bytes(bundle).decode("utf-8"),
                )

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "requires kqueue or inotify",
    )
    def test_review_transactions_reuse_one_monitor_and_skip_ignored_dependencies(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            _run_test_git(repository, "init", "--quiet")
            (repository / ".gitignore").write_text(
                "node_modules/\n",
                encoding="utf-8",
            )
            (repository / "allowed.txt").write_text(
                "authoritative review text\n",
                encoding="utf-8",
            )
            _run_test_git(repository, "add", ".gitignore", "allowed.txt")
            _run_test_git(repository, "commit", "--quiet", "-m", "transactions")
            dependency_tree = repository / "node_modules" / "dependency"
            dependency_tree.mkdir(parents=True)
            for index in range(32):
                (dependency_tree / f"artifact-{index}.js").write_text(
                    "ignored dependency\n",
                    encoding="utf-8",
                )
            commit = _run_test_git(repository, "rev-parse", "HEAD")
            tree = _run_test_git(repository, "rev-parse", "HEAD^{tree}")
            claim = SimpleNamespace(
                lane="architecture",
                repository_commit=commit,
                repository_tree=tree,
            )
            real_monitor = (
                verify_av1_cold_start_preregistration._ReviewGitAuthorityMonitor
            )
            instances: list[object] = []
            watched_snapshots: list[tuple[str, ...]] = []

            class TrackingMonitor(real_monitor):
                def __init__(self, authority: object) -> None:
                    self._recorded_for_test = False
                    instances.append(self)
                    super().__init__(authority)

                def close(self) -> None:
                    if not self._recorded_for_test:
                        watched_snapshots.append(tuple(self._watched_paths))
                        self._recorded_for_test = True
                    super().close()

            with (
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_ReviewGitAuthorityMonitor",
                    TrackingMonitor,
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_ALLOWLIST",
                    {"architecture": ("allowed.txt",)},
                ),
            ):
                self.assertEqual(
                    verify_av1_cold_start_preregistration._repository_review_identity(
                        process_controller=ManagedProcessController(),
                        repository_root=repository,
                    ),
                    (commit, tree),
                )
                bundle = verify_av1_cold_start_preregistration._build_av1_validation_derivation_review_bundle(
                    claim=claim,
                    process_controller=ManagedProcessController(),
                    repository_root=repository,
                )

            self.assertEqual(bundle["repository_commit"], commit)
            self.assertEqual(len(instances), 2)
            self.assertEqual(len(watched_snapshots), 2)
            dependency_prefix = f"{dependency_tree}{os.sep}"
            self.assertTrue(all(
                not any(
                    path == str(dependency_tree)
                    or path.startswith(dependency_prefix)
                    for path in watched_paths
                )
                for watched_paths in watched_snapshots
            ))

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "requires kqueue or inotify",
    )
    def test_linked_worktree_monitor_ignores_sibling_metadata_but_detects_current_churn(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            current_worktree = root / "current-worktree"
            sibling_worktree = root / "sibling-worktree"
            repository.mkdir()
            _run_test_git(repository, "init", "--quiet")
            (repository / "tracked.txt").write_text(
                "authoritative\n",
                encoding="utf-8",
            )
            _run_test_git(repository, "add", "tracked.txt")
            _run_test_git(repository, "commit", "--quiet", "-m", "worktrees")
            for worktree in (current_worktree, sibling_worktree):
                _run_test_git(
                    repository,
                    "worktree",
                    "add",
                    "--detach",
                    "--quiet",
                    str(worktree),
                    "HEAD",
                )

            authority = verify_av1_cold_start_preregistration._review_git_authority_snapshot(
                current_worktree
            )
            monitor = verify_av1_cold_start_preregistration._ReviewGitAuthorityMonitor(
                authority
            )
            sibling_git_directory, _common_directory = (
                verify_av1_cold_start_preregistration._review_git_authority(
                    sibling_worktree
                )
            )
            current_git_directory = authority[1][0]
            sibling_head = sibling_git_directory / "HEAD"
            current_head = current_git_directory / "HEAD"
            sibling_payload = sibling_head.read_bytes()
            current_payload = current_head.read_bytes()
            try:
                sibling_head.write_bytes(b"0" * 40 + b"\n")
                sibling_head.write_bytes(sibling_payload)
                monitor.assert_quiet()

                current_head.write_bytes(b"1" * 40 + b"\n")
                current_head.write_bytes(current_payload)
                with self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "Git authority changed",
                ):
                    monitor.assert_quiet()
            finally:
                sibling_head.write_bytes(sibling_payload)
                current_head.write_bytes(current_payload)
                monitor.close()

    def test_repository_monitor_reserves_runtime_descriptors_and_restores_owned_limit(
            self,
    ) -> None:
        resource = __import__("resource")

        def monitor_fixture() -> object:
            monitor = object.__new__(
                verify_av1_cold_start_preregistration._RepositoryAuthorityMonitor
            )
            monitor._os = os
            monitor._failure_type = RuntimeError
            monitor._unavailable_message = "unavailable"
            monitor._failure_message = None
            monitor._descriptors = []
            monitor._watched_paths = set()
            monitor._darwin_watcher = None
            monitor._inotify_descriptor = -1
            monitor._nofile_resource = None
            monitor._nofile_baseline_soft_limit = None
            monitor._original_nofile_soft_limit = None
            monitor._raised_nofile_soft_limit = None
            monitor._open_descriptor_count = lambda: 10
            return monitor

        for external_change in (False, True):
            with self.subTest(external_change=external_change):
                state = [256, 4096]
                set_calls: list[tuple[int, int]] = []

                def getrlimit(_resource: int) -> tuple[int, int]:
                    return state[0], state[1]

                def setrlimit(_resource: int, limits: tuple[int, int]) -> None:
                    set_calls.append(limits)
                    state[:] = limits

                monitor = monitor_fixture()
                with (
                    patch.object(resource, "getrlimit", side_effect=getrlimit),
                    patch.object(resource, "setrlimit", side_effect=setrlimit),
                ):
                    monitor._ensure_descriptor_capacity(300)
                    self.assertEqual(state[0], 557)
                    monitor._darwin_watcher = SimpleNamespace(close=lambda: None)
                    monitor._descriptors = [-1] * 300
                    monitor._open_descriptor_count = lambda: 311
                    monitor._ensure_descriptor_capacity(50)
                    raised_limit = state[0]
                    self.assertEqual(raised_limit, 607)
                    if external_change:
                        state[0] = raised_limit + 1
                    monitor._darwin_watcher = None
                    monitor._descriptors = []
                    monitor.close()

                self.assertEqual(set_calls[:2], [(557, 4096), (607, 4096)])
                if external_change:
                    self.assertEqual(set_calls, [(557, 4096), (607, 4096)])
                    self.assertEqual(state[0], raised_limit + 1)
                else:
                    self.assertEqual(set_calls[-1], (256, 4096))
                    self.assertEqual(state[0], 256)

        state = [256, 4096]
        set_calls = []

        def nested_getrlimit(_resource: int) -> tuple[int, int]:
            return state[0], state[1]

        def nested_setrlimit(_resource: int, limits: tuple[int, int]) -> None:
            set_calls.append(limits)
            state[:] = limits

        with (
            patch.object(resource, "getrlimit", side_effect=nested_getrlimit),
            patch.object(resource, "setrlimit", side_effect=nested_setrlimit),
        ):
            outer = monitor_fixture()
            outer._ensure_descriptor_capacity(300)
            inner = monitor_fixture()
            inner._ensure_descriptor_capacity(200)
            inner.close()
            outer.close()

        self.assertEqual(
            set_calls,
            [(557, 4096), (758, 4096), (557, 4096), (256, 4096)],
        )
        self.assertEqual(state[0], 256)

        state = [256, 400]
        set_calls = []
        monitor = monitor_fixture()
        with (
            patch.object(resource, "getrlimit", side_effect=nested_getrlimit),
            patch.object(resource, "setrlimit", side_effect=nested_setrlimit),
            self.assertRaisesRegex(RuntimeError, "unavailable"),
        ):
            monitor._ensure_descriptor_capacity(300)
        monitor.close()
        self.assertEqual(set_calls, [])

        state = [1499, 4096]
        set_calls = []
        monitor = monitor_fixture()
        monitor._open_descriptor_count = lambda: 1458
        with (
            patch.object(resource, "getrlimit", side_effect=nested_getrlimit),
            patch.object(resource, "setrlimit", side_effect=nested_setrlimit),
        ):
            monitor._ensure_descriptor_capacity(1293)
            self.assertEqual(state[0], 2880)
            monitor.close()
        self.assertEqual(set_calls, [(2880, 4096), (1499, 4096)])
        self.assertEqual(state[0], 1499)

        state = [resource.RLIM_INFINITY, resource.RLIM_INFINITY]
        set_calls = []
        monitor = monitor_fixture()
        with (
            patch.object(resource, "getrlimit", side_effect=nested_getrlimit),
            patch.object(resource, "setrlimit", side_effect=nested_setrlimit),
        ):
            monitor._ensure_descriptor_capacity(1)
            state[0] = 64
            monitor._ensure_descriptor_capacity(100)
            self.assertEqual(state[0], 239)
            monitor.close()
        self.assertEqual(
            set_calls,
            [(239, resource.RLIM_INFINITY), (64, resource.RLIM_INFINITY)],
        )
        self.assertEqual(state[0], 64)

        for error_number in (errno.EMFILE, errno.ENFILE, errno.ENOMEM):
            with self.subTest(error_number=error_number):
                monitor = monitor_fixture()
                monitor._stat = stat
                source_path = Path(__file__)
                source_state = monitor._state(source_path.stat())
                with (
                    patch.object(
                        os,
                        "open",
                        side_effect=OSError(error_number, "descriptor exhaustion"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "unavailable") as raised,
                ):
                    monitor._open_descriptor(str(source_path), source_state)
                self.assertEqual(raised.exception.__cause__.errno, error_number)

    def test_structured_response_and_evidence_reject_binding_decision_and_path_drift(
            self,
    ) -> None:
        proposal = self._candidate_proposal()
        claim = build_av1_validation_derivation_review_claim(
            plan=self.plan,
            proposal=proposal,
            repository_commit=REVIEW_REPOSITORY_COMMIT,
            repository_tree=REVIEW_REPOSITORY_TREE,
            lane="architecture",
            review_run_id="91000000-0000-0000-0000-000000000001",
            review_runner_canonical_path_sha256=self.plan.review_runner_canonical_path_sha256,
            review_runner_binary_sha256=self.plan.review_runner_binary_sha256,
            claimed_at="2026-07-28T03:00:01Z",
        )
        response = _structured_review_response(proposal, claim)
        self.assertEqual(
            validate_av1_validation_derivation_review_response(
                response,
                proposal=proposal,
                claim=claim,
            ),
            "approved",
        )
        response_pairs = list(response.items())
        for invalid_response in (
            response_pairs,
            [*response_pairs, ["decision", "rejected"]],
        ):
            with (
                self.subTest(invalid_response=invalid_response[-1]),
                self.assertRaisesRegex(
                    AV1ValidationDerivationError,
                    "response is invalid",
                ),
            ):
                verify_av1_cold_start_preregistration._structured_review_response(
                    json.dumps(invalid_response),
                    proposal=proposal,
                    claim=claim,
                )
        serialized_response = json.dumps(response, separators=(",", ":"))
        decision_member = '"decision":"approved"'
        self.assertEqual(serialized_response.count(decision_member), 1)
        duplicate_key_response = serialized_response.replace(
            decision_member,
            f'{decision_member},"decision":"rejected"',
            1,
        )
        self.assertEqual(duplicate_key_response.count('"decision":'), 2)
        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "response is invalid",
        ) as duplicate_error:
            verify_av1_cold_start_preregistration._structured_review_response(
                duplicate_key_response,
                proposal=proposal,
                claim=claim,
            )
        self.assertIsInstance(
            duplicate_error.exception.__cause__,
            AV1ValidationDerivationError,
        )
        self.assertIn(
            "duplicate JSON keys",
            str(duplicate_error.exception.__cause__),
        )
        binding_drift = dict(response)
        binding_drift["lane"] = "adversarial"
        with self.assertRaisesRegex(AV1ValidationDerivationError, "bindings"):
            validate_av1_validation_derivation_review_response(
                binding_drift,
                proposal=proposal,
                claim=claim,
            )
        invalid_findings = dict(response)
        invalid_findings["findings"] = None
        with self.assertRaisesRegex(AV1ValidationDerivationError, "findings"):
            validate_av1_validation_derivation_review_response(
                invalid_findings,
                proposal=proposal,
                claim=claim,
            )
        decision_drift = dict(response)
        decision_drift["decision"] = "rejected"
        with self.assertRaisesRegex(AV1ValidationDerivationError, "decision"):
            validate_av1_validation_derivation_review_response(
                decision_drift,
                proposal=proposal,
                claim=claim,
            )
        evidence = _review_run_evidence(proposal=proposal, claim=claim)
        review = build_av1_validation_derivation_review_attestation(
            proposal=proposal,
            claim=claim,
            review_evidence_sha256=f"sha256:{hashlib.sha256(evidence).hexdigest()}",
            decision="approved",
            reviewed_at="2026-07-28T03:00:02Z",
        )
        evidence_payload = json.loads(evidence)
        evidence_payload["safe_bundle"]["files"][0]["path"] = "path-drift.txt"
        drifted = canonical_json_bytes(evidence_payload)
        with self.assertRaisesRegex(AV1ValidationDerivationError, "path or size"):
            validate_av1_validation_derivation_review_run_evidence(drifted, review=review)

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

    def test_attempt_requires_all_persisted_review_clip_roles(self) -> None:
        assignment = self.plan.assignments[0]
        for role in ("preview_clips", "source_clips", "compare_clips"):
            calibration = _calibration_payload(
                assignment=assignment,
                source_identity=_source_identity(self.partition, assignment),
                crf=28.0,
                compatibility=_compatibility(assignment),
            )
            calibration[role] = []
            with self.subTest(role=role), self.assertRaisesRegex(
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

    def test_review_evidence_rejects_developer_binding_drift_with_matching_digest(self) -> None:
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
        drifted_developer_text = f"proposal_id={proposal.proposal_id}"
        evidence_payload["developer_text_sha256"] = (
            f"sha256:{hashlib.sha256(drifted_developer_text.encode('utf-8')).hexdigest()}"
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
            "structured review evidence bindings are invalid",
        ):
            validate_av1_validation_derivation_review_run_evidence(
                evidence,
                review=review,
            )

    def test_review_set_rejects_duplicate_substantive_analyses(self) -> None:
        proposal = self._candidate_proposal()
        claims: list[AV1ValidationDerivationReviewClaim] = []
        envelopes = []
        duplicate_analysis = (
            "The immutable candidate request is internally consistent across the "
            "frozen proposal, claim, repository identity, and bounded evidence bundle."
        )
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
                review_run_id=f"51000000-0000-0000-0000-{index:012x}",
                review_runner_canonical_path_sha256=(
                    self.plan.review_runner_canonical_path_sha256
                ),
                review_runner_binary_sha256=(
                    self.plan.review_runner_binary_sha256
                ),
                claimed_at=f"2026-07-28T03:00:{index:02d}Z",
            )
            evidence_payload = json.loads(
                _review_run_evidence(proposal=proposal, claim=claim)
            )
            if index <= 2:
                evidence_payload["model_response"]["analysis"] = duplicate_analysis
                evidence_payload["analysis_sha256"] = (
                    f"sha256:{hashlib.sha256(duplicate_analysis.casefold().encode('utf-8')).hexdigest()}"
                )
                evidence_payload["model_response_sha256"] = (
                    "sha256:"
                    f"{hashlib.sha256(canonical_json_bytes(evidence_payload['model_response'])).hexdigest()}"
                )
            evidence = canonical_json_bytes(evidence_payload)
            review = build_av1_validation_derivation_review_attestation(
                proposal=proposal,
                claim=claim,
                review_evidence_sha256=(
                    f"sha256:{hashlib.sha256(evidence).hexdigest()}"
                ),
                decision="approved",
                reviewed_at=f"2026-07-28T03:{index:02d}:00Z",
            )
            claims.append(claim)
            envelopes.append(build_av1_validation_derivation_review_envelope(
                review=review,
                evidence=evidence,
            ))

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "not independent",
        ):
            _av1_validation_derivation_review_set_sha256(
                self.plan,
                claims,
                envelopes,
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

    def test_checkout_drift_does_not_strand_orphaned_assignment_claim(self) -> None:
        assignment = self.plan.assignments[0]
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        write_av1_validation_derivation_assignment_claim(
            attempts_dir,
            assignment_id=assignment.assignment_id,
            plan_id=self.plan.plan_id,
            authorization_id=self.plan.authorization.authorization_id,
            claimed_at="2026-07-28T01:00:00Z",
        )

        with (
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.open_readonly_db",
                side_effect=AssertionError("live inventory must not be read"),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_execution_contract",
                side_effect=AssertionError("execution drift must not be checked"),
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "interrupted state was terminalized",
            ),
        ):
            _run_av1_validation_derivation_assignment_locked(
                config=self.runtime_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=self._drifted_repository_identity,
                assignment_id=assignment.assignment_id,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                now_iso=lambda: "2026-07-28T01:01:00Z",
            )

        attempt = load_av1_validation_derivation_attempts(attempts_dir)[0]
        terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
        self.assertEqual(attempt.status, "stopped")
        self.assertEqual(attempt.reason_code, "interrupted_claim")
        self.assertEqual(terminal.attempt_id, attempt.attempt_id)

    def test_interrupted_nonreview_attempt_recovery_ignores_checkout_drift(self) -> None:
        attempt = self._failed_attempt(self.plan.assignments[0].assignment_id)
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        write_av1_validation_derivation_attempt(attempts_dir, attempt)

        def fail_on_live_repository_check() -> None:
            raise AV1ValidationDerivationError("repository snapshot drifted")

        self.assertTrue(_recover_interrupted_derivation_state(
            plan=self.plan,
            partition=self.partition,
            artifact_root=self.runtime_artifact_root,
            attempts_directory=attempts_dir,
            terminal_records_directory=records_dir,
            completed_at="2026-07-28T01:06:00Z",
            before_observed_publish=fail_on_live_repository_check,
        ))
        terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.reason_code, "runtime_failure")
        self.assertEqual(terminal.attempt_id, attempt.attempt_id)

    def test_interrupted_verdict_claim_recovery_ignores_checkout_drift(self) -> None:
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

        def fail_on_live_repository_check() -> None:
            raise AV1ValidationDerivationError("repository snapshot drifted")

        self.assertTrue(_recover_interrupted_derivation_state(
            plan=self.plan,
            partition=self.partition,
            artifact_root=self.runtime_artifact_root,
            attempts_directory=attempts_dir,
            terminal_records_directory=records_dir,
            completed_at="2026-07-28T01:07:00Z",
            before_observed_publish=fail_on_live_repository_check,
        ))
        terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        self.assertEqual(terminal.attempt_id, attempt.attempt_id)

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
            JUST_BEFORE_VALID_UNTIL,
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
                now_iso=lambda: JUST_BEFORE_VALID_UNTIL,
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

        def record_publication(
                label: str,
                kwargs: dict[str, object],
        ) -> None:
            before_publish = kwargs.get("before_publish")
            self.assertTrue(callable(before_publish))
            assert callable(before_publish)
            before_publish()
            events.append(label)

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
                side_effect=lambda *_args, **kwargs: record_publication(
                    "attempt",
                    kwargs,
                ),
            ),
            patch(
                "mediaforce.web.runtime.av1_validation_derivation.write_av1_validation_derivation_terminal_record",
                side_effect=lambda *_args, **kwargs: record_publication(
                    "terminal-record",
                    kwargs,
                ),
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

    def test_assignment_attributes_unexpected_quality_search_failure(self) -> None:
        self._assert_assignment_runtime_dependency_failure(
            dependency="search_quality_for_source",
            reason_code="runtime_quality_search_failure",
        )

    def test_assignment_attributes_unexpected_crop_detection_failure(self) -> None:
        self._assert_assignment_runtime_dependency_failure(
            dependency="detect_video_crop",
            reason_code="runtime_crop_detection_failure",
        )

    def test_assignment_attributes_unexpected_toolchain_failure(self) -> None:
        self._assert_assignment_runtime_dependency_failure(
            dependency="quality_toolchain_identity",
            reason_code="runtime_toolchain_failure",
        )

    def test_assignment_attributes_unexpected_sample_encode_failure(self) -> None:
        self._assert_assignment_runtime_dependency_failure(
            dependency="run_sample_encode",
            reason_code="runtime_sample_encode_failure",
        )

    def test_assignment_attributes_unexpected_review_generation_failure(self) -> None:
        self._assert_assignment_runtime_dependency_failure(
            dependency="encode_preview_clips",
            reason_code="runtime_review_generation_failure",
        )

    def test_assignment_attributes_unexpected_preflight_failure(self) -> None:
        attempt, terminal = self._run_assignment_with_runtime_and_cleanup_result(
            failure_dependency=None,
            cleanup_failure=False,
            preflight_failure=True,
        )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.reason_code, "runtime_preflight_failure")
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.reason_code, "runtime_preflight_failure")

    def test_assignment_attributes_unexpected_source_snapshot_failure(self) -> None:
        attempt, terminal = self._run_assignment_with_runtime_and_cleanup_result(
            failure_dependency=None,
            cleanup_failure=False,
            source_snapshot_failure=True,
        )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(
            attempt.reason_code,
            "runtime_source_snapshot_failure",
        )
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(
            terminal.reason_code,
            "runtime_source_snapshot_failure",
        )

    def test_assignment_preserves_primary_failure_when_cleanup_also_fails(self) -> None:
        attempt, terminal = self._run_assignment_with_runtime_and_cleanup_result(
            failure_dependency="search_quality_for_source",
            cleanup_failure=True,
        )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(
            attempt.reason_code,
            "runtime_quality_search_failure",
        )
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(
            terminal.reason_code,
            "runtime_quality_search_failure",
        )
        self.assertNotIn(
            "private cleanup failure detail",
            canonical_json_bytes(attempt.to_payload()).decode("utf-8"),
        )

    def test_assignment_restores_stage_after_successful_dependency(self) -> None:
        attempt, terminal = self._run_assignment_with_runtime_and_cleanup_result(
            failure_dependency=None,
            failure_after_dependency="detect_video_crop",
            cleanup_failure=False,
        )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.reason_code, "runtime_preflight_failure")
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.reason_code, "runtime_preflight_failure")

    def test_assignment_attributes_cleanup_failure_after_success(self) -> None:
        attempt, terminal = self._run_assignment_with_runtime_and_cleanup_result(
            failure_dependency=None,
            cleanup_failure=True,
        )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.reason_code, "runtime_cleanup_failure")
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.reason_code, "runtime_cleanup_failure")

    def test_assignment_cleanup_failure_at_deadline_is_authorization_expired(self) -> None:
        attempt, terminal = self._run_assignment_with_runtime_and_cleanup_result(
            failure_dependency=None,
            cleanup_failure=True,
            cleanup_crosses_authorization=True,
        )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.reason_code, "authorization_expired")
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.reason_code, "authorization_expired")

    def test_assignment_successful_cleanup_at_deadline_is_authorization_expired(self) -> None:
        attempt, terminal = self._run_assignment_with_runtime_and_cleanup_result(
            failure_dependency=None,
            cleanup_failure=False,
            cleanup_crosses_authorization=True,
        )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.reason_code, "authorization_expired")
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.reason_code, "authorization_expired")

    def test_assignment_attributes_malformed_calibration_result(self) -> None:
        attempt, terminal = self._run_assignment_with_runtime_and_cleanup_result(
            failure_dependency=None,
            cleanup_failure=False,
            malformed_result=True,
        )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(
            attempt.reason_code,
            "runtime_result_validation_failure",
        )
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(
            terminal.reason_code,
            "runtime_result_validation_failure",
        )

    def test_assignment_attributes_activity_guard_enter_failure(self) -> None:
        attempt, terminal = self._run_assignment_with_runtime_and_cleanup_result(
            failure_dependency=None,
            cleanup_failure=False,
            activity_guard_failure="enter",
        )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.reason_code, "runtime_preflight_failure")
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.reason_code, "runtime_preflight_failure")

    def test_assignment_attributes_activity_guard_exit_failure(self) -> None:
        attempt, terminal = self._run_assignment_with_runtime_and_cleanup_result(
            failure_dependency=None,
            cleanup_failure=False,
            activity_guard_failure="exit",
        )

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.reason_code, "runtime_cleanup_failure")
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.reason_code, "runtime_cleanup_failure")

    def test_assignment_activity_guard_exits_when_cleanup_is_interrupted(self) -> None:
        activity_guard_events: list[str] = []

        with self.assertRaises(KeyboardInterrupt):
            self._run_assignment_with_runtime_and_cleanup_result(
                failure_dependency=None,
                cleanup_failure=False,
                cleanup_base_exception=True,
                activity_guard_events=activity_guard_events,
            )

        self.assertEqual(activity_guard_events, ["enter", "exit"])

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
        timestamps = iter((JUST_BEFORE_VALID_UNTIL, VALID_UNTIL))
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

    def test_expired_frozen_verdict_intent_is_terminalized(self) -> None:
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

        self.assertTrue(_recover_interrupted_derivation_state(
            plan=self.plan,
            partition=self.partition,
            artifact_root=self.runtime_artifact_root,
            attempts_directory=attempts_dir,
            terminal_records_directory=records_dir,
            completed_at=AFTER_VALID_UNTIL,
        ))

        terminal = load_av1_validation_derivation_terminal_records(records_dir)[0]
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.reason_code, "authorization_expired")
        self.assertEqual(terminal.attempt_id, attempt.attempt_id)

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

    def test_favorable_terminal_intent_recovery_keeps_repository_and_expiry_guards(self) -> None:
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
        terminal_intent_path = (
            self.runtime_artifact_root
            / "terminal-intents"
            / f"{terminal.assignment_id}.json"
        )
        terminal_intent_inode = terminal_intent_path.stat().st_ino

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "repository snapshot drifted",
        ):
            _run_av1_validation_derivation_assignment_locked(
                config=self.runtime_config,
                manifest=self.manifest,
                partition=self.partition,
                token_key=self.token_key,
                plan=self.plan,
                repository_identity_resolver=self._drifted_repository_identity,
                assignment_id=assignment.assignment_id,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                now_iso=lambda: "2026-07-28T01:07:00Z",
            )
        self.assertFalse(records_dir.exists())

        with patch(
            "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
            side_effect=lambda info: (
                10**30
                if info.st_ino == terminal_intent_inode
                else info.st_ctime_ns
            ),
        ):
            self.assertTrue(_recover_interrupted_derivation_state(
                plan=self.plan,
                partition=self.partition,
                artifact_root=self.runtime_artifact_root,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                completed_at="2026-07-28T01:07:00Z",
                before_observed_publish=lambda: None,
            ))
        recovered = load_av1_validation_derivation_terminal_records(records_dir)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].status, "failed")
        self.assertEqual(recovered[0].reason_code, "authorization_expired")

    def test_late_terminal_intent_is_validated_before_rollback(self) -> None:
        assignment = self.plan.assignments[0]
        attempt = self._review_pending_attempt()
        stale_attempt = build_av1_validation_derivation_attempt(
            plan=self.plan,
            partition=self.partition,
            assignment_id=assignment.assignment_id,
            started_at="2026-07-28T00:59:00Z",
            completed_at="2026-07-28T01:05:00Z",
            status="review_pending",
            calibration_payload=_calibration_payload(
                assignment=assignment,
                source_identity=_source_identity(self.partition, assignment),
                crf=28.0,
                compatibility=_compatibility(assignment),
            ),
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
        stale_terminal = build_av1_validation_derivation_terminal_record(
            plan=self.plan,
            partition=self.partition,
            attempt=stale_attempt,
            observation=_observation(
                assignment=assignment,
                source_identity=_source_identity(self.partition, assignment),
                crf=28.0,
                bitrate=1_000_000,
                verdict="acceptable",
            ),
        )
        terminal_intents_directory = (
            self.runtime_artifact_root / "terminal-intents"
        )
        ensure_av1_validation_derivation_terminal_intent(
            terminal_intents_directory,
            stale_terminal,
        )
        terminal_intent_path = (
            terminal_intents_directory
            / f"{stale_terminal.assignment_id}.json"
        )
        terminal_intent_inode = terminal_intent_path.stat().st_ino

        with (
            patch(
                "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
                side_effect=lambda info: (
                    10**30
                    if info.st_ino == terminal_intent_inode
                    else info.st_ctime_ns
                ),
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "terminal intent does not match its attempt",
            ),
        ):
            _recover_interrupted_derivation_state(
                plan=self.plan,
                partition=self.partition,
                artifact_root=self.runtime_artifact_root,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                completed_at=AFTER_VALID_UNTIL,
            )

        self.assertTrue(terminal_intent_path.is_file())
        self.assertFalse(records_dir.exists())

    def test_recovery_rejects_existing_observed_terminal_pair_published_after_expiry(
            self,
    ) -> None:
        assignment = self.plan.assignments[0]
        attempt = self._review_pending_attempt()
        terminal = build_av1_validation_derivation_terminal_record(
            plan=self.plan,
            partition=self.partition,
            attempt=attempt,
            observation=_observation(
                assignment=assignment,
                source_identity=_source_identity(self.partition, assignment),
                crf=28.0,
                bitrate=1_000_000,
                verdict="acceptable",
            ),
        )
        attempts_dir = self.runtime_artifact_root / "attempts"
        records_dir = self.runtime_artifact_root / "terminal-records"
        write_av1_validation_derivation_attempt(attempts_dir, attempt)
        ensure_av1_validation_derivation_terminal_intent(
            self.runtime_artifact_root / "terminal-intents",
            terminal,
        )
        ensure_av1_validation_derivation_terminal_record(
            records_dir,
            terminal,
        )

        with (
            patch(
                "mediaforce.tuning.av1_validation_derivation._owner_only_publication_time_ns",
                return_value=10**30,
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "published after authorization expired",
            ),
        ):
            _recover_interrupted_derivation_state(
                plan=self.plan,
                partition=self.partition,
                artifact_root=self.runtime_artifact_root,
                attempts_directory=attempts_dir,
                terminal_records_directory=records_dir,
                completed_at="2026-07-28T01:07:00Z",
                before_observed_publish=lambda: None,
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
            before_observed_publish=lambda: None,
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
                repository_identity_resolver=self._matching_repository_identity,
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
                repository_identity_resolver=self._matching_repository_identity,
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
                        operation_kwargs: dict[str, object] = {
                            "config_path": Path("unused.toml"),
                            "manifest": self.manifest,
                            "partition": self.partition,
                            "token_key": self.token_key,
                            "plan_path": plan_path,
                            "cell_plan_id": (
                                self.plan.assignments[0].cell_plan_id
                            ),
                        }
                        if operation is finalize_runtime_av1_validation_derivation_candidate_lock:
                            operation_kwargs["repository_identity_resolver"] = (
                                self._matching_repository_identity
                            )
                        else:
                            operation_kwargs["repository_commit"] = (
                                self.plan.repository_commit
                            )
                            operation_kwargs["repository_tree"] = (
                                self.plan.repository_tree
                            )
                        operation(**operation_kwargs)

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
            clip_payload = {
                "path": clip_path.as_uri(),
                "timestamp_seconds": 1.0,
                "duration_seconds": 8.0,
            }
            calibration = {
                "preview_clips": [dict(clip_payload)],
                "source_clips": [dict(clip_payload)],
                "compare_clips": [dict(clip_payload)],
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
            clip_payload = {
                "path": clip_path.as_uri(),
                "timestamp_seconds": 1.0,
                "duration_seconds": 8.0,
            }
            calibration = {
                "preview_clips": [dict(clip_payload)],
                "source_clips": [dict(clip_payload)],
                "compare_clips": [dict(clip_payload)],
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
                role: [{
                    "path": clip_paths[index].as_uri(),
                    "timestamp_seconds": float(index),
                    "duration_seconds": 8.0,
                }]
                for index, role in enumerate(
                    ("preview_clips", "source_clips", "compare_clips")
                )
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
            clip_payload = {
                "path": clip_path.as_uri(),
                "timestamp_seconds": 0.0,
                "duration_seconds": 8.0,
            }
            calibration = {
                "preview_clips": [dict(clip_payload)],
                "source_clips": [dict(clip_payload)],
                "compare_clips": [dict(clip_payload)],
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

    def test_review_fingerprint_requires_preview_source_and_compare_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifact-root"
            artifact_root.mkdir(mode=0o700)
            review_root = _prepare_derivation_review_root(artifact_root)
            clip_path = review_root / "encoded-01.mp4"
            clip_path.write_bytes(b"review")
            clip_path.chmod(0o400)
            clip_payload = {
                "path": clip_path.as_uri(),
                "timestamp_seconds": 0.0,
                "duration_seconds": 8.0,
            }

            self.assertIsNone(_current_derivation_review_artifact_fingerprint(
                review_root=review_root,
                calibration={
                    "preview_clips": [dict(clip_payload)],
                    "source_clips": [dict(clip_payload)],
                    "compare_clips": [],
                },
            ))

    def test_review_fingerprint_reports_cleanup_failure_inside_outer_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory) / "artifact-root"
            artifact_root.mkdir(mode=0o700)
            review_root = _prepare_derivation_review_root(artifact_root)
            clip_path = review_root / "encoded-01.mp4"
            clip_path.write_bytes(b"review")
            clip_path.chmod(0o400)
            clip_payload = {
                "path": clip_path.as_uri(),
                "timestamp_seconds": 0.0,
                "duration_seconds": 8.0,
            }
            calibration = {
                "preview_clips": [dict(clip_payload)],
                "source_clips": [dict(clip_payload)],
                "compare_clips": [dict(clip_payload)],
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
            clip_payload = {
                "path": clip_path.as_uri(),
                "timestamp_seconds": 0.0,
                "duration_seconds": 8.0,
            }
            calibration = {
                "preview_clips": [dict(clip_payload)],
                "source_clips": [dict(clip_payload)],
                "compare_clips": [dict(clip_payload)],
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
                repository_identity_resolver=self._matching_repository_identity,
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
                repository_identity_resolver=self._matching_repository_identity,
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
                repository_identity_resolver=self._matching_repository_identity,
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
        clock_values = iter((JUST_BEFORE_VALID_UNTIL, VALID_UNTIL))
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
                repository_identity_resolver=self._matching_repository_identity,
                now_iso=clock,
            )
        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        self.assertEqual(
            clock_calls,
            [JUST_BEFORE_VALID_UNTIL, VALID_UNTIL],
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

    def test_derivation_verdict_rejects_expired_favorable_commit_after_intent(
            self,
    ) -> None:
        committed = False
        rolled_back = False

        @contextmanager
        def database(
                _path: Path,
                *,
                before_commit: Callable[[], None] | None = None,
        ) -> Iterator[SimpleNamespace]:
            nonlocal committed, rolled_back
            connection = SimpleNamespace(exec_driver_sql=lambda _sql: None)
            try:
                yield connection
                assert before_commit is not None
                before_commit()
                committed = True
            except BaseException:
                rolled_back = True
                raise

        clock_values = iter((
            "2026-07-31T23:59:55Z",
            "2026-07-31T23:59:56Z",
            "2026-07-31T23:59:57Z",
            "2026-07-31T23:59:58Z",
            VALID_UNTIL,
        ))
        clock_calls: list[str] = []

        def clock() -> str:
            value = next(clock_values)
            clock_calls.append(value)
            return value

        with self.assertRaisesRegex(
            AV1ValidationDerivationError,
            "outside its authorization window",
        ):
            self._record_approved_derivation_verdict(
                database=database,
                clock=clock,
            )

        self.assertFalse(committed)
        self.assertTrue(rolled_back)
        self.assertEqual(clock_calls, [
            "2026-07-31T23:59:55Z",
            "2026-07-31T23:59:56Z",
            "2026-07-31T23:59:57Z",
            "2026-07-31T23:59:58Z",
            VALID_UNTIL,
        ])
        terminal = load_av1_validation_derivation_terminal_records(
            self.runtime_artifact_root / "terminal-records"
        )[0]
        self.assertEqual(terminal.status, "observed")
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

    def test_derivation_verdict_stops_before_expired_favorable_terminal_publish(
            self,
    ) -> None:
        committed = False
        rolled_back = False

        @contextmanager
        def database(
                _path: Path,
                *,
                before_commit: Callable[[], None] | None = None,
        ) -> Iterator[SimpleNamespace]:
            nonlocal committed, rolled_back
            connection = SimpleNamespace(exec_driver_sql=lambda _sql: None)
            try:
                yield connection
                assert before_commit is not None
                before_commit()
                committed = True
            except BaseException:
                rolled_back = True
                raise

        clock_values = iter((
            "2026-07-31T23:59:58Z",
            JUST_BEFORE_VALID_UNTIL,
            VALID_UNTIL,
        ))
        clock_calls: list[str] = []

        def clock() -> str:
            value = next(clock_values)
            clock_calls.append(value)
            return value

        terminal = self._record_approved_derivation_verdict(
            database=database,
            clock=clock,
        )

        self.assertEqual(terminal.status, "stopped")
        self.assertEqual(terminal.reason_code, "safety_stop")
        self.assertFalse(committed)
        self.assertTrue(rolled_back)
        self.assertEqual(clock_calls, [
            "2026-07-31T23:59:58Z",
            JUST_BEFORE_VALID_UNTIL,
            VALID_UNTIL,
        ])
        self.assertEqual(
            load_av1_validation_derivation_terminal_records(
                self.runtime_artifact_root / "terminal-records"
            ),
            (terminal,),
        )
        self.assertEqual(
            load_av1_validation_derivation_terminal_intents(
                self.runtime_artifact_root / "terminal-intents"
            ),
            (terminal,),
        )

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
                repository_identity_resolver=self._matching_repository_identity,
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
                repository_identity_resolver=self._matching_repository_identity,
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
                repository_identity_resolver=self._matching_repository_identity,
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
                repository_identity_resolver=self._matching_repository_identity,
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

        def fail_terminal_intent(*_args: object, **kwargs: object) -> None:
            before_publish = kwargs.get("before_publish")
            self.assertTrue(callable(before_publish))
            assert callable(before_publish)
            before_publish()
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
                repository_identity_resolver=self._matching_repository_identity,
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
            "source-final-quiet",
            "observed-terminal-intent",
            "source-final-quiet",
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
        expired_retry_clock_calls = 0

        def expired_retry_clock() -> str:
            nonlocal expired_retry_clock_calls
            expired_retry_clock_calls += 1
            return VALID_UNTIL

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
                    repository_identity_resolver=self._matching_repository_identity,
                    recorded_at="2026-07-28T01:06:00Z",
                    now_iso=lambda: "2026-07-28T01:06:00Z",
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
                repository_identity_resolver=self._matching_repository_identity,
                recorded_at="2026-07-28T01:06:00Z",
                now_iso=expired_retry_clock,
            )
        self.assertEqual(database_calls, 2)
        self.assertEqual(expired_retry_clock_calls, 0)
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
                    repository_identity_resolver=self._matching_repository_identity,
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
                repository_identity_resolver=self._matching_repository_identity,
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
                repository_identity_resolver=self._matching_repository_identity,
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
        repository_checks = 0

        def resolve_repository_identity() -> tuple[str, str]:
            nonlocal repository_checks
            repository_checks += 1
            return self._matching_repository_identity()

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

        def freeze_verdict_intent(
                *_args: object,
                **kwargs: object,
        ) -> dict[str, object]:
            before_publish = kwargs.get("before_publish")
            self.assertTrue(callable(before_publish))
            assert callable(before_publish)
            before_publish()
            record_event("verdict-intent")
            return {
                "verdict": "approved",
                "concern_tags": [],
                "evidence_ids": [],
                "moment_indexes": [],
                "recorded_at": "2026-07-28T01:06:00Z",
            }

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
                repository_identity_resolver=resolve_repository_identity,
                recorded_at="2026-07-28T01:06:00Z",
                now_iso=lambda: "2026-07-28T01:06:00Z",
            )
        self.assertEqual(terminal.status, "observed")
        self.assertGreaterEqual(repository_checks, 9)
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
            "source-quiet",
            "terminal-intent",
            "source-quiet",
            "terminal-record",
            "source-quiet",
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
                repository_identity_resolver=self._matching_repository_identity,
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
                    repository_identity_resolver=self._matching_repository_identity,
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
                        repository_identity_resolver=self._matching_repository_identity,
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
            proposed_at=JUST_BEFORE_VALID_UNTIL,
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
                side_effect=(JUST_BEFORE_VALID_UNTIL, VALID_UNTIL),
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
                repository_identity_resolver=self._matching_repository_identity,
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

    def test_proposal_rechecks_live_repository_inside_publication_callback(
            self,
    ) -> None:
        cell_plan_id = self.plan.assignments[0].cell_plan_id
        proposal = self._candidate_proposal(
            proposed_at="2026-07-28T03:00:00Z",
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
        live_identity = [self.plan.repository_commit, self.plan.repository_tree]
        repository_checks = 0

        def resolve_repository_identity() -> tuple[str, str]:
            nonlocal repository_checks
            repository_checks += 1
            return live_identity[0], live_identity[1]

        def publish_proposal(*_args: object, **kwargs: object) -> None:
            before_publish = kwargs.get("before_publish")
            self.assertTrue(callable(before_publish))
            assert callable(before_publish)
            live_identity[:] = ["b" * 40, "c" * 40]
            before_publish()
            self.fail("repository-drifted proposal reached publication")

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
                return_value="2026-07-28T03:00:00Z",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "write_av1_validation_derivation_candidate_proposal",
                side_effect=publish_proposal,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_print_partition_payload",
            ),
            self.assertRaisesRegex(
                AV1ValidationDerivationError,
                "repository snapshot drifted",
            ),
        ):
            verify_av1_cold_start_preregistration._run_derivation_proposal_action(
                args,
                config=self.runtime_config,
                repository_identity_resolver=resolve_repository_identity,
            )

        self.assertEqual(repository_checks, 2)
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
        repository_checks = 0
        expected_envelope = SimpleNamespace(payload_sha256="sha256:test")

        def resolve_repository_identity() -> tuple[str, str]:
            nonlocal repository_checks
            repository_checks += 1
            return self._matching_repository_identity()

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
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_observed_attempts_accepted",
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
                repository_identity_resolver=resolve_repository_identity,
                now_iso=sample_clock,
            )
        self.assertIs(envelope, expected_envelope)
        self.assertGreaterEqual(repository_checks, 5)
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
                "mediaforce.web.runtime.av1_validation_derivation.assert_av1_validation_derivation_observed_attempts_accepted",
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
                repository_identity_resolver=self._matching_repository_identity,
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
            self.assertEqual(now_iso.call_count, 5)
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

    def test_create_plan_cli_capacity_failure_does_not_publish_plan(self) -> None:
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
            current_inputs = (
                self.manifest,
                self.partition,
                self.token_key,
                _SourceSHA256Session(),
                self.source_commitments,
            )
            total_source_size_bytes = sum(
                commitment.source_size_bytes
                for commitment in self.source_commitments
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
            stderr = io.StringIO()
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
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "_review_runner_identity",
                    return_value=(
                        root / "private-code-runner",
                        self.plan.review_runner_canonical_path_sha256,
                        self.plan.review_runner_binary_sha256,
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
                patch(
                    "mediaforce.web.runtime.av1_validation_derivation._available_bytes_for_descriptor",
                    return_value=(
                        total_source_size_bytes
                        + AV1_VALIDATION_DERIVATION_MINIMUM_FREE_BYTES
                        - 1
                    ),
                ),
                redirect_stderr(stderr),
            ):
                exit_code = verify_av1_cold_start_preregistration.main(argv)
            self.assertEqual(exit_code, 1)
            self.assertIn("cumulative source snapshot capacity", stderr.getvalue())
            artifact_root = (
                runtime_config.paths.web_state_dir
                / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
                / self.partition.partition_id
            )
            self.assertFalse((artifact_root / "plan.json").exists())
            self.assertFalse((artifact_root / ".binding").exists())
            self.assertTrue((artifact_root / "source-snapshots").is_dir())

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
    clip = {
        "path": f"file:///private/{assignment.assignment_id}.mp4",
        "timestamp_seconds": 0.0,
        "duration_seconds": 8.0,
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
        "preview_clips": [dict(clip)],
        "source_clips": [dict(clip)],
        "compare_clips": [dict(clip)],
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
    safe_bundle = _review_bundle_for_claim(claim)
    request = build_av1_validation_derivation_review_request(
        proposal=proposal,
        claim=claim,
        safe_bundle=safe_bundle,
    )
    developer_text = av1_validation_derivation_review_developer_text(claim.lane)
    response_schema = build_av1_validation_derivation_review_response_schema(
        proposal=proposal,
        claim=claim,
    )
    model_response = _structured_review_response(proposal, claim, decision=decision)
    return canonical_json_bytes({
        "schema": AV1_VALIDATION_DERIVATION_STRUCTURED_REVIEW_RUN_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA_VERSION,
        "review_run_id": claim.review_run_id,
        "reviewer_token": claim.reviewer_token,
        "proposal_id": proposal.proposal_id,
        "proposal_payload_sha256": proposal.payload_sha256,
        "repository_commit": claim.repository_commit,
        "repository_tree": claim.repository_tree,
        "review_claim_id": claim.claim_id,
        "review_claim_payload_sha256": claim.payload_sha256,
        "lane": claim.lane,
        "decision": decision,
        "review_runner_canonical_path_sha256": claim.review_runner_canonical_path_sha256,
        "review_runner_binary_sha256": claim.review_runner_binary_sha256,
        "proposal": proposal.to_payload(),
        "review_claim": claim.to_payload(),
        "safe_bundle": safe_bundle,
        "request": request,
        "request_sha256": f"sha256:{hashlib.sha256(canonical_json_bytes(request)).hexdigest()}",
        "developer_text": developer_text,
        "developer_text_sha256": (
            f"sha256:{hashlib.sha256(developer_text.encode('utf-8')).hexdigest()}"
        ),
        "response_schema": response_schema,
        "response_schema_sha256": (
            f"sha256:{hashlib.sha256(canonical_json_bytes(response_schema)).hexdigest()}"
        ),
        "model_response": model_response,
        "model_response_sha256": (
            f"sha256:{hashlib.sha256(canonical_json_bytes(model_response)).hexdigest()}"
        ),
        "analysis_sha256": av1_validation_derivation_review_analysis_sha256(
            model_response
        ),
        "returncode": 0,
        "stderr_sha256": f"sha256:{hashlib.sha256(b'').hexdigest()}",
    })


def _review_bundle_for_claim(claim: object) -> dict[str, object]:
    lane = str(getattr(claim, "lane"))
    files: list[dict[str, object]] = []
    for ordinal, path in enumerate(
            AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_ALLOWLIST[lane],
            start=1,
    ):
        text = f"Fixture review bundle for {lane}: {path}.\n"
        encoded = text.encode("utf-8")
        files.append({
            "path": path,
            "blob_id": f"{ordinal:040x}",
            "blob_size_bytes": len(encoded),
            "blob_sha256": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
            "text": text,
        })
    semantic_payload = {
        "schema": AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA_VERSION,
        "lane": lane,
        "repository_commit": str(getattr(claim, "repository_commit")),
        "repository_tree": str(getattr(claim, "repository_tree")),
        "files": files,
    }
    return {
        **semantic_payload,
        "payload_sha256": (
            f"sha256:{hashlib.sha256(canonical_json_bytes(semantic_payload)).hexdigest()}"
        ),
    }


def _structured_review_response(
        proposal: AV1ValidationDerivationCandidateProposal,
        claim: AV1ValidationDerivationReviewClaim,
        *,
        decision: Literal["approved", "rejected"] = "approved",
) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    if decision == "rejected":
        findings.append({
            "severity": "blocking",
            "summary": "The frozen authorization binding is incomplete.",
            "basis": "The candidate cannot be approved until its immutable request and proposal bindings agree.",
        })
    return {
        "schema": "mediaforce.av1_derivation_review_response",
        "schema_version": 1,
        "decision": decision,
        "lane": claim.lane,
        "proposal_id": proposal.proposal_id,
        "proposal_payload_sha256": proposal.payload_sha256,
        "repository_commit": claim.repository_commit,
        "repository_tree": claim.repository_tree,
        "review_claim_id": claim.claim_id,
        "review_claim_payload_sha256": claim.payload_sha256,
        "review_run_id": claim.review_run_id,
        "analysis": (
            f"The {claim.lane} lane independently compared the immutable proposal, "
            "claim, and bounded repository bundle. The frozen identifiers, measured "
            "constraints, authorization window, and review scope are internally "
            "consistent, and no blocking discrepancy was found in this fixture."
        ),
        "findings": findings,
    }


def _structured_review_response_from_request(
        request: dict[str, object],
) -> dict[str, object]:
    return {
        "schema": "mediaforce.av1_derivation_review_response",
        "schema_version": 1,
        "decision": "approved",
        "lane": request["lane"],
        "proposal_id": request["proposal_id"],
        "proposal_payload_sha256": request["proposal_payload_sha256"],
        "repository_commit": request["repository_commit"],
        "repository_tree": request["repository_tree"],
        "review_claim_id": request["review_claim_id"],
        "review_claim_payload_sha256": request["review_claim_payload_sha256"],
        "review_run_id": request["review_run_id"],
        "analysis": (
            "The immutable request binds the lane, proposal, review claim, repository "
            "commit, and bounded tracked-blob bundle. The fixture found no blocking "
            "discrepancy in those frozen values and therefore approves this candidate."
        ),
        "findings": [],
    }


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
