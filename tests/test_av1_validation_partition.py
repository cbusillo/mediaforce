import copy
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mediaforce.core.file_integrity import FileIntegrityError, rename_exclusive
from mediaforce.core.utils import content_version_fingerprint
from mediaforce.tuning.av1_validation_partition import (
    AV1ValidationPartitionError,
    AV1ValidationPartitionExpectations,
    AV1ValidationPartitionSource,
    AV1ValidationPrivatePartition,
    _ordered_partition_slots,
    _partition_slots,
    _select_sources,
    av1_validation_partition_key_id,
    av1_validation_private_partition_from_payload,
    assert_private_artifact_path,
    av1_validation_partition_public_summary,
    build_av1_validation_private_partition,
    create_av1_validation_partition_key,
    ensure_av1_validation_partition_key,
    load_av1_validation_partition_key,
    load_av1_validation_private_partition,
    validate_av1_validation_partition_current_inputs,
    validate_av1_validation_private_partition,
    write_av1_validation_private_partition,
)
from mediaforce.tuning.av1_validation_partition_inventory import (
    av1_validation_partition_source_sha256_resolver,
)
from mediaforce.tuning.av1_validation_v2 import load_av1_validation_manifest_v2
from mediaforce.web.runtime_lock import (
    MediaforceRuntimeLockOwnershipError,
    exclusive_mediaforce_runtime_lock,
)
from scripts import verify_av1_cold_start_preregistration


V2_MANIFEST_PATH = Path("docs/validation/av1-cold-start-preregistration-v2.json")
SELECTED_AT = "2026-07-27T22:50:00Z"


def _summary_sha256(summary: object) -> str:
    payload = json.dumps(summary, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


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


class AV1ValidationPartitionTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_directory = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_directory.cleanup)
        runtime_root = Path(runtime_directory.name)
        runtime_config = SimpleNamespace(
            paths=SimpleNamespace(
                config_path=runtime_root / "config.toml",
                db_path=runtime_root / "mediaforce.sqlite3",
                web_state_dir=runtime_root / "state",
            )
        )
        runtime_lock = exclusive_mediaforce_runtime_lock(
            runtime_config,
            owner_payload={"purpose": "partition-test"},
        )
        runtime_lock.__enter__()
        self.addCleanup(runtime_lock.__exit__, None, None, None)
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
        self.token_key_id = av1_validation_partition_key_id(self.token_key)

    def test_partition_covers_exact_slots_and_has_no_execution_authority(self) -> None:
        partition = self._build()
        self.assertEqual(partition.holdout_count, 50)
        self.assertEqual(partition.derivation_reservation_count, 24)
        self.assertEqual(len(partition.assignments), 74)
        self.assertEqual(
            len({assignment.source_token for assignment in partition.assignments}),
            74,
        )
        self.assertEqual(
            len({assignment.title_token for assignment in partition.assignments}),
            74,
        )
        self.assertEqual(
            len({assignment.series_token for assignment in partition.assignments}),
            74,
        )
        self.assertEqual(
            len(
                {assignment.source_group_token for assignment in partition.assignments}
            ),
            74,
        )
        summary = av1_validation_partition_public_summary(partition)
        self.assertFalse(summary["runtime_execution_authorized"])
        self.assertFalse(summary["derivation_execution_authorized"])
        self.assertFalse(summary["holdout_execution_authorized"])
        self.assertNotIn("local_item_id", json.dumps(summary))
        self.assertNotIn("source_token", json.dumps(summary))

    def test_assignment_payload_keys_match_frozen_schema_1(self) -> None:
        assignment = self._build().assignments[0]
        token_keys = {
            "assignment_id",
            "role",
            "cell_plan_id",
            "ordinal",
            "traits",
            "intent_level",
            "source_token",
            "title_token",
            "series_token",
            "source_group_token",
            "compatibility_signature",
            "policy_signature",
            "target_video_bitrate_bps",
            "quality_metric",
            "quality_target",
            "minimum_quality_score",
        }
        self.assertEqual(set(assignment.token_payload()), token_keys)
        self.assertEqual(
            set(assignment.to_payload()),
            token_keys | {"local_item_id", "evidence_summary_sha256"},
        )

    def test_partition_identity_is_independent_of_uncommitted_source_bytes(self) -> None:
        self.assertEqual(self._build(), self._build())

    def test_partition_schema_1_locked_digests_are_golden(self) -> None:
        partition = self._build()
        self.assertEqual(
            partition.selection_lock_sha256,
            "sha256:e6b9445911f829d3ae41b9ba7e06d271ab89fcf2727b572142a97729b2d38843",
        )
        self.assertEqual(
            partition.derivation_partition_sha256,
            "sha256:f540d936f3200daed4ba77289bddd78ad9fa7b7ae2f1788aa230f8773c2e3a04",
        )

    def test_partition_publication_checks_sources_before_atomic_visibility(self) -> None:
        partition = self._build()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partition.json"

            def reject_publication() -> None:
                raise AV1ValidationPartitionError(
                    "AV1 partition selected source changed after verification"
                )

            with self.assertRaisesRegex(
                AV1ValidationPartitionError,
                "changed after verification",
            ):
                write_av1_validation_private_partition(
                    path,
                    partition,
                    before_publish=reject_publication,
                )

            self.assertFalse(path.exists())
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_partition_publication_recovers_visible_matching_artifact(self) -> None:
        partition = self._build()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partition.json"

            def publish_then_fail(**kwargs: object) -> None:
                rename_exclusive(**kwargs)
                raise OSError("simulated post-rename durability failure")

            with (
                patch(
                    "mediaforce.tuning.av1_validation_partition.rename_exclusive",
                    side_effect=publish_then_fail,
                ),
                self.assertRaisesRegex(
                    AV1ValidationPartitionError,
                    "could not be written safely",
                ),
            ):
                write_av1_validation_private_partition(path, partition)

            self.assertEqual(
                load_av1_validation_private_partition(path),
                partition,
            )
            write_av1_validation_private_partition(path, partition)

    def test_partition_key_recovers_visible_valid_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partition.key"

            def publish_then_fail(**kwargs: object) -> None:
                rename_exclusive(**kwargs)
                raise OSError("simulated post-rename durability failure")

            with (
                patch(
                    "mediaforce.tuning.av1_validation_partition.rename_exclusive",
                    side_effect=publish_then_fail,
                ),
                self.assertRaisesRegex(
                    AV1ValidationPartitionError,
                    "could not be written safely",
                ),
            ):
                create_av1_validation_partition_key(path)

            expected_key_id = av1_validation_partition_key_id(
                load_av1_validation_partition_key(path)
            )
            self.assertEqual(
                create_av1_validation_partition_key(path),
                expected_key_id,
            )
            recovered_key_id, created = ensure_av1_validation_partition_key(path)
            self.assertEqual(recovered_key_id, expected_key_id)
            self.assertFalse(created)

    def test_partition_key_collision_reports_existing_key_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partition.key"
            competing_key = b"c" * 32

            def publish_competing_key(**kwargs: object) -> None:
                descriptor = os.open(
                    str(kwargs["destination_name"]),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=int(kwargs["destination_directory_descriptor"]),
                )
                try:
                    os.write(descriptor, competing_key)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                raise FileExistsError

            with patch(
                "mediaforce.tuning.av1_validation_partition.rename_exclusive",
                side_effect=publish_competing_key,
            ):
                token_key_id, created = ensure_av1_validation_partition_key(path)

            self.assertFalse(created)
            self.assertEqual(
                token_key_id,
                av1_validation_partition_key_id(competing_key),
            )

    def test_partition_key_read_rejects_visible_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "partition.key"
            replacement = root / "replacement.key"
            path.write_bytes(b"a" * 32)
            replacement.write_bytes(b"b" * 32)
            path.chmod(0o600)
            replacement.chmod(0o600)
            real_read = os.read
            replaced = False

            def read_then_replace(descriptor: int, size: int) -> bytes:
                nonlocal replaced
                chunk = real_read(descriptor, size)
                if not replaced:
                    os.replace(replacement, path)
                    replaced = True
                return chunk

            with (
                patch(
                    "mediaforce.tuning.av1_validation_partition.os.read",
                    side_effect=read_then_replace,
                ),
                self.assertRaisesRegex(
                    AV1ValidationPartitionError,
                    "stable regular file|changed while it was read",
                ),
            ):
                load_av1_validation_partition_key(path)

    def test_partition_cleanup_failure_is_not_suppressed(self) -> None:
        partition = self._build()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partition.json"

            def reject_publication() -> None:
                raise AV1ValidationPartitionError("simulated source drift")

            with (
                patch(
                    "mediaforce.tuning.av1_validation_partition.os.unlink",
                    side_effect=OSError("simulated unlink failure"),
                ),
                self.assertRaisesRegex(
                    AV1ValidationPartitionError,
                    "cleanup failed",
                ),
            ):
                write_av1_validation_private_partition(
                    path,
                    partition,
                    before_publish=reject_publication,
                )

    def test_partition_expectations_allow_merged_zero_quality_floor(self) -> None:
        expectations = replace(self.expectations, minimum_quality_score=0.0)
        self.assertEqual(expectations.minimum_quality_score, 0.0)

    def test_partition_is_deterministic_for_shuffled_inventory(self) -> None:
        expected = self._build()
        actual = build_av1_validation_private_partition(
            manifest=self.manifest,
            eligibility_attestation_id=self.manifest.eligibility_attestation_id,
            eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
            sources=tuple(reversed(self.sources)),
            expectations=self.expectations,
            token_key=self.token_key,
            expected_token_key_id=self.token_key_id,
            selected_at=SELECTED_AT,
        )
        self.assertEqual(actual, expected)
        validate_av1_validation_private_partition(
            actual,
            manifest=self.manifest,
            token_key=self.token_key,
        )
        validate_av1_validation_partition_current_inputs(
            actual,
            manifest=self.manifest,
            sources=self.sources,
            expectations=self.expectations,
            token_key=self.token_key,
        )

    def test_partition_rejects_selection_outside_manifest_window(self) -> None:
        registered_at = datetime.fromisoformat(
            self.manifest.registered_at.replace("Z", "+00:00")
        )
        valid_until = datetime.fromisoformat(
            self.manifest.valid_until.replace("Z", "+00:00")
        )
        partition = build_av1_validation_private_partition(
            manifest=self.manifest,
            eligibility_attestation_id=self.manifest.eligibility_attestation_id,
            eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
            sources=self.sources,
            expectations=self.expectations,
            token_key=self.token_key,
            expected_token_key_id=self.token_key_id,
            selected_at=self.manifest.registered_at,
        )
        self.assertEqual(partition.selected_at, self.manifest.registered_at)

        invalid_timestamps = (
            (
                registered_at - timedelta(seconds=1),
                "predates manifest registration",
            ),
            (valid_until, "must precede manifest expiration"),
            (
                valid_until + timedelta(seconds=1),
                "must precede manifest expiration",
            ),
        )
        for timestamp, message in invalid_timestamps:
            selected_at = (
                timestamp.astimezone(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
            with self.subTest(selected_at=selected_at):
                with self.assertRaisesRegex(AV1ValidationPartitionError, message):
                    build_av1_validation_private_partition(
                        manifest=self.manifest,
                        eligibility_attestation_id=(
                            self.manifest.eligibility_attestation_id
                        ),
                        eligibility_payload_sha256=(
                            self.manifest.eligibility_payload_sha256
                        ),
                        sources=self.sources,
                        expectations=self.expectations,
                        token_key=self.token_key,
                        expected_token_key_id=self.token_key_id,
                        selected_at=selected_at,
                    )

    def test_derivation_selection_cannot_remap_frozen_holdouts(self) -> None:
        candidates_by_plan = {
            plan.cell_plan_id: tuple(
                source
                for source in self.sources
                if plan.trait_selector.matches(source.traits)
            )
            for plan in self.manifest.cell_plans
        }
        holdout_slots = _ordered_partition_slots(
            _partition_slots(self.manifest),
            candidates_by_plan=candidates_by_plan,
            role="holdout",
        )
        holdout_selection = _select_sources(
            slots=holdout_slots,
            candidates_by_plan=candidates_by_plan,
            manifest_id=self.manifest.manifest_id,
            token_key=self.token_key,
        )
        partition_holdouts = {
            assignment.assignment_id: assignment.local_item_id
            for assignment in self._build().assignments
            if assignment.role == "holdout"
        }
        self.assertEqual(
            partition_holdouts,
            {
                assignment_id: source.local_item_id
                for assignment_id, source in holdout_selection.items()
            },
        )

    def test_partition_rejects_a_key_that_does_not_match_the_precommitment(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            AV1ValidationPartitionError,
            "precommitted token-key ID",
        ):
            build_av1_validation_private_partition(
                manifest=self.manifest,
                eligibility_attestation_id=self.manifest.eligibility_attestation_id,
                eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
                sources=self.sources,
                expectations=self.expectations,
                token_key=self.token_key,
                expected_token_key_id="av1vkey1_not_the_committed_key",
                selected_at=SELECTED_AT,
            )

    def test_current_input_validation_rejects_a_narrowed_inventory(self) -> None:
        partition = self._build()
        selected_ids = {
            assignment.local_item_id for assignment in partition.assignments
        }
        narrowed = tuple(
            source for source in self.sources if source.local_item_id in selected_ids
        )
        narrowed_partition = build_av1_validation_private_partition(
            manifest=self.manifest,
            eligibility_attestation_id=self.manifest.eligibility_attestation_id,
            eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
            sources=narrowed,
            expectations=self.expectations,
            token_key=self.token_key,
            expected_token_key_id=self.token_key_id,
            selected_at=SELECTED_AT,
        )
        validate_av1_validation_private_partition(
            narrowed_partition,
            manifest=self.manifest,
            token_key=self.token_key,
        )
        with self.assertRaisesRegex(
            AV1ValidationPartitionError,
            "does not match current inventory or policy inputs",
        ):
            validate_av1_validation_partition_current_inputs(
                narrowed_partition,
                manifest=self.manifest,
                sources=self.sources,
                expectations=self.expectations,
                token_key=self.token_key,
            )

    def test_exact_candidate_selector_rejects_trait_supersets(self) -> None:
        widened = tuple(
            replace(source, traits=("darkness", "motion"))
            if source.traits == ("motion",)
            else source
            for source in self.sources
        )
        with self.assertRaisesRegex(
            AV1ValidationPartitionError, "lacks distinct eligible series"
        ):
            build_av1_validation_private_partition(
                manifest=self.manifest,
                eligibility_attestation_id=self.manifest.eligibility_attestation_id,
                eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
                sources=widened,
                expectations=self.expectations,
                token_key=self.token_key,
                expected_token_key_id=self.token_key_id,
                selected_at=SELECTED_AT,
            )

    def test_partition_fails_closed_when_global_series_disjointness_is_impossible(
        self,
    ) -> None:
        repeated_series = tuple(
            replace(source, series_identity="darkness-shared-series")
            if source.traits == ("darkness",)
            else source
            for source in self.sources
        )
        with self.assertRaisesRegex(
            AV1ValidationPartitionError, "lacks distinct eligible series"
        ):
            build_av1_validation_private_partition(
                manifest=self.manifest,
                eligibility_attestation_id=self.manifest.eligibility_attestation_id,
                eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
                sources=repeated_series,
                expectations=self.expectations,
                token_key=self.token_key,
                expected_token_key_id=self.token_key_id,
                selected_at=SELECTED_AT,
            )

    def test_partition_rejects_candidate_source_group_concentration(self) -> None:
        darkness_index = 0
        concentrated = []
        for source in self.sources:
            if source.traits != ("darkness",):
                concentrated.append(source)
                continue
            darkness_index += 1
            group = (
                f"darkness-minority-{darkness_index}"
                if darkness_index <= 11
                else "darkness-dominant"
            )
            concentrated.append(replace(source, source_group_identity=group))
        with self.assertRaisesRegex(
            AV1ValidationPartitionError,
            "lacks source-group concentration capacity",
        ):
            build_av1_validation_private_partition(
                manifest=self.manifest,
                eligibility_attestation_id=self.manifest.eligibility_attestation_id,
                eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
                sources=tuple(concentrated),
                expectations=self.expectations,
                token_key=self.token_key,
                expected_token_key_id=self.token_key_id,
                selected_at=SELECTED_AT,
            )

    def test_partition_rejects_fewer_than_six_candidate_source_groups(self) -> None:
        grouped = tuple(
            replace(
                source,
                source_group_identity=f"darkness-group-{source.local_item_id % 5}",
            )
            if source.traits == ("darkness",)
            else source
            for source in self.sources
        )
        with self.assertRaisesRegex(
            AV1ValidationPartitionError,
            "lacks disjoint candidate source-group diversity",
        ):
            build_av1_validation_private_partition(
                manifest=self.manifest,
                eligibility_attestation_id=self.manifest.eligibility_attestation_id,
                eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
                sources=grouped,
                expectations=self.expectations,
                token_key=self.token_key,
                expected_token_key_id=self.token_key_id,
                selected_at=SELECTED_AT,
            )

    def test_partition_rejects_duplicate_content_versions(self) -> None:
        duplicate = replace(
            self.sources[-1],
            source_identity=self.sources[0].source_identity,
        )
        with self.assertRaisesRegex(
            AV1ValidationPartitionError,
            "repeats source identities",
        ):
            build_av1_validation_private_partition(
                manifest=self.manifest,
                eligibility_attestation_id=self.manifest.eligibility_attestation_id,
                eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
                sources=(*self.sources[:-1], duplicate),
                expectations=self.expectations,
                token_key=self.token_key,
                expected_token_key_id=self.token_key_id,
                selected_at=SELECTED_AT,
            )

    def test_partition_filters_policy_incompatible_sources(self) -> None:
        incompatible = tuple(
            replace(source, base_policy_signature="av1vbasepolicy1_incompatible")
            if source.traits == ("motion",)
            else source
            for source in self.sources
        )
        with self.assertRaisesRegex(
            AV1ValidationPartitionError, "lacks distinct eligible series"
        ):
            build_av1_validation_private_partition(
                manifest=self.manifest,
                eligibility_attestation_id=self.manifest.eligibility_attestation_id,
                eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
                sources=incompatible,
                expectations=self.expectations,
                token_key=self.token_key,
                expected_token_key_id=self.token_key_id,
                selected_at=SELECTED_AT,
            )

    def test_private_key_and_partition_are_owner_only_and_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private_dir = Path(directory) / "private"
            private_dir.mkdir(mode=0o700)
            key_path = private_dir / "partition.key"
            partition_path = private_dir / "partition.json"
            create_av1_validation_partition_key(key_path)
            key = load_av1_validation_partition_key(key_path)
            partition = build_av1_validation_private_partition(
                manifest=self.manifest,
                eligibility_attestation_id=self.manifest.eligibility_attestation_id,
                eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
                sources=self.sources,
                expectations=self.expectations,
                token_key=key,
                expected_token_key_id=av1_validation_partition_key_id(key),
                selected_at=SELECTED_AT,
            )
            write_av1_validation_private_partition(partition_path, partition)
            self.assertEqual(os.stat(key_path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(partition_path).st_mode & 0o777, 0o600)
            self.assertEqual(
                load_av1_validation_private_partition(partition_path), partition
            )

            key_link = private_dir / "partition-key-link"
            partition_link = private_dir / "partition-link.json"
            key_link.symlink_to(key_path)
            partition_link.symlink_to(partition_path)
            with self.assertRaisesRegex(AV1ValidationPartitionError, "regular file"):
                load_av1_validation_partition_key(key_link)
            with self.assertRaisesRegex(AV1ValidationPartitionError, "regular file"):
                load_av1_validation_private_partition(partition_link)

    def test_schema_1_reads_accept_historical_parent_modes_and_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private_dir = Path(directory) / "private"
            private_dir.mkdir(mode=0o700)
            key_path = private_dir / "partition.key"
            partition_path = private_dir / "partition.json"
            create_av1_validation_partition_key(key_path)
            key = load_av1_validation_partition_key(key_path)
            partition = build_av1_validation_private_partition(
                manifest=self.manifest,
                eligibility_attestation_id=self.manifest.eligibility_attestation_id,
                eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
                sources=self.sources,
                expectations=self.expectations,
                token_key=key,
                expected_token_key_id=av1_validation_partition_key_id(key),
                selected_at=SELECTED_AT,
            )
            write_av1_validation_private_partition(partition_path, partition)
            key_hardlink = private_dir / "partition-hardlink.key"
            partition_hardlink = private_dir / "partition-hardlink.json"
            os.link(key_path, key_hardlink)
            os.link(partition_path, partition_hardlink)
            private_dir.chmod(0o755)

            self.assertEqual(load_av1_validation_partition_key(key_hardlink), key)
            self.assertEqual(
                load_av1_validation_private_partition(partition_hardlink),
                partition,
            )

    def test_private_artifact_path_rejects_repository_contents(self) -> None:
        with self.assertRaisesRegex(
            AV1ValidationPartitionError, "outside the repository"
        ):
            assert_private_artifact_path(
                Path("docs/validation/private-partition.json"),
                repository_root=Path.cwd(),
            )

    def test_private_partition_parser_rejects_unknown_keys_and_algorithm_drift(
        self,
    ) -> None:
        payload = copy.deepcopy(self._build().to_payload())
        payload["unexpected"] = True
        with self.assertRaisesRegex(AV1ValidationPartitionError, "keys are invalid"):
            av1_validation_private_partition_from_payload(payload)

        payload = copy.deepcopy(self._build().to_payload())
        payload["selection_lock"]["assignment_algorithm"] = "unreviewed_algorithm"
        with self.assertRaisesRegex(
            AV1ValidationPartitionError,
            "algorithm or token version is invalid",
        ):
            av1_validation_private_partition_from_payload(payload)

    def _build(self) -> AV1ValidationPrivatePartition:
        return build_av1_validation_private_partition(
            manifest=self.manifest,
            eligibility_attestation_id=self.manifest.eligibility_attestation_id,
            eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
            sources=self.sources,
            expectations=self.expectations,
            token_key=self.token_key,
            expected_token_key_id=self.token_key_id,
            selected_at=SELECTED_AT,
        )


class AV1ValidationPartitionSourceDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        if hasattr(__import__("select"), "kqueue"):
            return
        integrity_guard_patcher = patch(
            "mediaforce.tuning.av1_validation_partition_inventory.MacOSFileIntegrityGuard",
            new=_DescriptorBindingFileIntegrityGuard,
        )
        integrity_guard_patcher.start()
        self.addCleanup(integrity_guard_patcher.stop)

    def test_selected_source_digest_replays_exact_fingerprint_evidence(self) -> None:
        summary = {
            "schema_version": 1,
            "analysis": {"sampled_frames": 24},
            "decision": {"status": "measured", "traits": ["typical"]},
        }
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.mkv"
            source_bytes = b"registered-source" * 80_000
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            source = _digest_test_source(
                source_identity=source_identity,
                evidence_summary_sha256=_summary_sha256(summary),
            )
            connection = _digest_test_connection(
                source=source,
                source_path=source_path,
                source_size_bytes=len(source_bytes),
            )
            with patch(
                "mediaforce.tuning.av1_validation_partition_inventory.probe_evidence",
                return_value=summary,
            ) as probe:
                with av1_validation_partition_source_sha256_resolver(
                    connection,
                    config=SimpleNamespace(),
                    verify_evidence=True,
                ) as resolver:
                    source_sha256 = resolver(source)
                    source_size_bytes = resolver.source_size_bytes(source)
                    resolver.verify()
            self.assertEqual(
                source_sha256,
                f"sha256:{hashlib.sha256(source_bytes).hexdigest()}",
            )
            self.assertEqual(source_size_bytes, len(source_bytes))
            self.assertEqual(probe.call_args.args[0], source_path.resolve())

    def test_selected_source_digest_rejects_unsampled_evidence_drift(self) -> None:
        stored_summary = {
            "schema_version": 1,
            "analysis": {"sampled_frames": 24},
            "decision": {"status": "measured", "traits": ["typical"]},
        }
        fresh_summary = {
            "schema_version": 1,
            "analysis": {"sampled_frames": 24},
            "decision": {"status": "measured", "traits": ["motion"]},
        }
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.mkv"
            source_bytes = bytearray(b"A" * (1024 * 1024))
            source_path.write_bytes(source_bytes)
            source_identity = content_version_fingerprint(
                source_path,
                source_path.stat(),
            )
            source_bytes[128 * 1024] = ord("B")
            source_path.write_bytes(source_bytes)
            self.assertEqual(
                content_version_fingerprint(source_path, source_path.stat()),
                source_identity,
            )
            source = _digest_test_source(
                source_identity=source_identity,
                evidence_summary_sha256=_summary_sha256(stored_summary),
            )
            connection = _digest_test_connection(
                source=source,
                source_path=source_path,
                source_size_bytes=len(source_bytes),
            )
            with (
                patch(
                    "mediaforce.tuning.av1_validation_partition_inventory.probe_evidence",
                    return_value=fresh_summary,
                ),
                self.assertRaisesRegex(
                    AV1ValidationPartitionError,
                    "evidence does not replay from its frozen bytes",
                ),
            ):
                with av1_validation_partition_source_sha256_resolver(
                    connection,
                    config=SimpleNamespace(),
                    verify_evidence=True,
                ) as resolver:
                    resolver(source)

    def test_selected_sources_remain_guarded_as_one_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "first.mkv"
            second_path = root / "second.mkv"
            first_bytes = b"first-source" * 80_000
            second_bytes = b"second-source" * 80_000
            first_path.write_bytes(first_bytes)
            second_path.write_bytes(second_bytes)
            first_source = _digest_test_source(
                local_item_id=1,
                source_identity=content_version_fingerprint(
                    first_path,
                    first_path.stat(),
                ),
                evidence_summary_sha256=f"sha256:{'1' * 64}",
            )
            second_source = _digest_test_source(
                local_item_id=2,
                source_identity=content_version_fingerprint(
                    second_path,
                    second_path.stat(),
                ),
                evidence_summary_sha256=f"sha256:{'2' * 64}",
            )
            first_connection = _digest_test_connection(
                source=first_source,
                source_path=first_path,
                source_size_bytes=len(first_bytes),
            )
            second_connection = _digest_test_connection(
                source=second_source,
                source_path=second_path,
                source_size_bytes=len(second_bytes),
            )
            connection = Mock()
            connection.execute.side_effect = [
                first_connection.execute.return_value,
                second_connection.execute.return_value,
            ]
            with self.assertRaisesRegex(
                AV1ValidationPartitionError,
                "cohort validation|integrity monitoring failed",
            ):
                with av1_validation_partition_source_sha256_resolver(
                    connection,
                    config=SimpleNamespace(),
                ) as resolver:
                    resolver(first_source)
                    first_path.write_bytes(b"changed-source" * 80_000)
                    first_path.write_bytes(first_bytes)
                    resolver(second_source)
                    resolver.verify()

    def test_selected_source_mutation_after_verification_fails_context_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.mkv"
            source_bytes = b"registered-source" * 80_000
            source_path.write_bytes(source_bytes)
            source = _digest_test_source(
                source_identity=content_version_fingerprint(
                    source_path,
                    source_path.stat(),
                ),
                evidence_summary_sha256=f"sha256:{'1' * 64}",
            )
            connection = _digest_test_connection(
                source=source,
                source_path=source_path,
                source_size_bytes=len(source_bytes),
            )

            with self.assertRaisesRegex(
                AV1ValidationPartitionError,
                "cohort validation|integrity monitoring failed",
            ):
                with av1_validation_partition_source_sha256_resolver(
                    connection,
                    config=SimpleNamespace(),
                ) as resolver:
                    resolver(source)
                    resolver.verify()
                    source_path.write_bytes(b"Z" * len(source_bytes))

    def test_selected_source_exit_validation_runs_when_publication_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.mkv"
            source_bytes = b"registered-source" * 80_000
            source_path.write_bytes(source_bytes)
            source = _digest_test_source(
                source_identity=content_version_fingerprint(
                    source_path,
                    source_path.stat(),
                ),
                evidence_summary_sha256=f"sha256:{'1' * 64}",
            )
            connection = _digest_test_connection(
                source=source,
                source_path=source_path,
                source_size_bytes=len(source_bytes),
            )

            with self.assertRaisesRegex(
                AV1ValidationPartitionError,
                "cohort validation|integrity monitoring failed",
            ):
                with av1_validation_partition_source_sha256_resolver(
                    connection,
                    config=SimpleNamespace(),
                ) as resolver:
                    resolver(source)
                    resolver.verify()
                    source_path.write_bytes(b"Z" * len(source_bytes))
                    raise RuntimeError("simulated publication failure")


class AV1ValidationPartitionLockOwnershipTests(unittest.TestCase):
    def test_partition_writers_reject_calls_without_runtime_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_path = root / "private" / "partition.key"
            partition_path = root / "private" / "partition.json"

            with self.assertRaises(MediaforceRuntimeLockOwnershipError):
                ensure_av1_validation_partition_key(key_path)
            with self.assertRaises(MediaforceRuntimeLockOwnershipError):
                write_av1_validation_private_partition(
                    partition_path,
                    Mock(spec=AV1ValidationPrivatePartition),
                )

            self.assertFalse(key_path.parent.exists())

    def test_partition_writers_accept_active_runtime_lock(self) -> None:
        manifest = load_av1_validation_manifest_v2(V2_MANIFEST_PATH)
        expectations = AV1ValidationPartitionExpectations(
            compatibility_signature="av1vcompat1_test_contract",
            base_policy_signature="av1vbasepolicy1_test_contract",
            quality_metric="vmaf",
            quality_target=85.0,
            minimum_quality_score=80.0,
        )
        token_key = b"k" * 32
        partition = build_av1_validation_private_partition(
            manifest=manifest,
            eligibility_attestation_id=manifest.eligibility_attestation_id,
            eligibility_payload_sha256=manifest.eligibility_payload_sha256,
            sources=_partition_sources(expectations),
            expectations=expectations,
            token_key=token_key,
            expected_token_key_id=av1_validation_partition_key_id(token_key),
            selected_at=SELECTED_AT,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                paths=SimpleNamespace(
                    config_path=root / "config.toml",
                    db_path=root / "mediaforce.sqlite3",
                    web_state_dir=root / "state",
                )
            )
            key_path = root / "private" / "partition.key"
            partition_path = root / "private" / "partition.json"
            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "partition-writer-test"},
            ):
                ensure_av1_validation_partition_key(key_path)
                write_av1_validation_private_partition(
                    partition_path,
                    partition,
                )

            self.assertTrue(key_path.is_file())
            self.assertTrue(partition_path.is_file())


class AV1ValidationPartitionCliTests(unittest.TestCase):
    def test_create_partition_key_holds_runtime_lock(self) -> None:
        lock_held = False
        config = SimpleNamespace()

        @contextmanager
        def runtime_lock(
                current_config: object,
                *,
                owner_payload: dict[str, object],
        ) -> Iterator[None]:
            nonlocal lock_held
            self.assertIs(current_config, config)
            self.assertEqual(owner_payload["purpose"], "av1-partition-key-create")
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

        def migrate(current_config: object) -> None:
            self.assertIs(current_config, config)
            self.assertTrue(lock_held)

        def create_key(_path: Path) -> tuple[str, bool]:
            self.assertTrue(lock_held)
            return "av1vkey1_test", True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with (
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "load_config",
                    return_value=config,
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "exclusive_mediaforce_runtime_lock",
                    side_effect=runtime_lock,
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "migrate_config_state",
                    side_effect=migrate,
                ),
                patch.object(
                    verify_av1_cold_start_preregistration,
                    "ensure_av1_validation_partition_key",
                    side_effect=create_key,
                ),
                redirect_stdout(output),
            ):
                exit_code = verify_av1_cold_start_preregistration.main([
                    "create-partition-key",
                    str(root / "partition.key"),
                    "--config",
                    str(root / "config.toml"),
                    "--json",
                ])

        self.assertEqual(exit_code, 0)
        self.assertFalse(lock_held)
        self.assertEqual(
            json.loads(output.getvalue())["token_key_id"],
            "av1vkey1_test",
        )
        self.assertTrue(json.loads(output.getvalue())["created"])

    def test_validate_eligibility_output_is_count_free(self) -> None:
        expected = {
            "eligibility_valid": True,
            "runtime_execution_authorized": False,
            "derivation_execution_authorized": False,
            "holdout_execution_authorized": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            attestation_path = Path(directory) / "eligibility.json"
            for json_output in (False, True):
                output = io.StringIO()
                argv = ["validate-eligibility", str(attestation_path)]
                if json_output:
                    argv.append("--json")
                with (
                    self.subTest(json_output=json_output),
                    patch.object(
                        verify_av1_cold_start_preregistration,
                        "load_av1_validation_v2_eligibility",
                        return_value=object(),
                    ),
                    patch.object(
                        verify_av1_cold_start_preregistration,
                        "assert_preregistered_av1_validation_v2_eligibility",
                    ),
                    redirect_stdout(output),
                ):
                    exit_code = verify_av1_cold_start_preregistration.main(argv)

                self.assertEqual(exit_code, 0)
                if json_output:
                    self.assertEqual(json.loads(output.getvalue()), expected)
                else:
                    self.assertEqual(
                        output.getvalue().strip(),
                        "eligibility_valid=true "
                        "runtime_execution_authorized=false "
                        "derivation_execution_authorized=false "
                        "holdout_execution_authorized=false",
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


def _digest_test_source(
    *,
    local_item_id: int = 1,
    source_identity: str,
    evidence_summary_sha256: str,
) -> AV1ValidationPartitionSource:
    return AV1ValidationPartitionSource(
        local_item_id=local_item_id,
        source_identity=source_identity,
        title_identity="title-digest-test",
        series_identity="series-digest-test",
        source_group_identity="group-digest-test",
        traits=("typical",),
        compatibility_signature="av1vcompat1_digest_test",
        base_policy_signature="av1vbasepolicy1_digest_test",
        target_video_bitrate_bps=1_000_000,
        quality_metric="vmaf",
        quality_target=85.0,
        minimum_quality_score=80.0,
        evidence_summary_sha256=evidence_summary_sha256,
    )


def _digest_test_connection(
    *,
    source: AV1ValidationPartitionSource,
    source_path: Path,
    source_size_bytes: int,
) -> Mock:
    result = Mock()
    result.mappings.return_value.one_or_none.return_value = {
        "id": source.local_item_id,
        "source_path": str(source_path),
        "rel_path": source_path.name,
        "media_root": "",
        "content_version_fingerprint": source.source_identity,
        "size_bytes": source_size_bytes,
    }
    connection = Mock()
    connection.execute.return_value = result
    return connection


if __name__ == "__main__":
    unittest.main()
