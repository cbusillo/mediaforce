import copy
from contextlib import redirect_stdout
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

from mediaforce.core.file_integrity import FileIntegrityError
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
from scripts import verify_av1_cold_start_preregistration


V2_MANIFEST_PATH = Path("docs/validation/av1-cold-start-preregistration-v2.json")
SELECTED_AT = "2026-07-27T22:50:00Z"


def _source_sha256(source: AV1ValidationPartitionSource) -> str:
    return f"sha256:{hashlib.sha256(source.source_identity.encode()).hexdigest()}"


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
        self.assertNotIn(partition.assignments[0].source_sha256, json.dumps(summary))

    def test_source_digest_changes_every_locked_partition_identity(self) -> None:
        expected = self._build()
        changed_item_id = expected.assignments[0].local_item_id

        def changed_source_sha256(source: AV1ValidationPartitionSource) -> str:
            if source.local_item_id == changed_item_id:
                return f"sha256:{'f' * 64}"
            return _source_sha256(source)

        actual = build_av1_validation_private_partition(
            manifest=self.manifest,
            eligibility_attestation_id=self.manifest.eligibility_attestation_id,
            eligibility_payload_sha256=self.manifest.eligibility_payload_sha256,
            sources=self.sources,
            expectations=self.expectations,
            token_key=self.token_key,
            expected_token_key_id=self.token_key_id,
            selected_at=SELECTED_AT,
            source_sha256_resolver=changed_source_sha256,
        )
        self.assertNotEqual(actual.selection_lock_sha256, expected.selection_lock_sha256)
        self.assertNotEqual(
            actual.derivation_partition_sha256,
            expected.derivation_partition_sha256,
        )
        self.assertNotEqual(actual.partition_id, expected.partition_id)
        self.assertNotEqual(actual.payload_sha256, expected.payload_sha256)

    def test_partition_without_frozen_source_digest_fails_closed(self) -> None:
        payload = copy.deepcopy(self._build().to_payload())
        del payload["assignments"][0]["source_sha256"]
        with self.assertRaisesRegex(
            AV1ValidationPartitionError,
            "assignment keys are invalid",
        ):
            av1_validation_private_partition_from_payload(payload)

    def test_partition_expectations_require_positive_quality_floor(self) -> None:
        with self.assertRaisesRegex(
            AV1ValidationPartitionError,
            "quality expectations",
        ):
            replace(self.expectations, minimum_quality_score=0.0)

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
            source_sha256_resolver=_source_sha256,
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
            source_sha256_resolver=_source_sha256,
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
                        source_sha256_resolver=_source_sha256,
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
                source_sha256_resolver=_source_sha256,
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
            source_sha256_resolver=_source_sha256,
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
                source_sha256_resolver=_source_sha256,
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
                source_sha256_resolver=_source_sha256,
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
                source_sha256_resolver=_source_sha256,
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
                source_sha256_resolver=_source_sha256,
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
                source_sha256_resolver=_source_sha256,
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
                source_sha256_resolver=_source_sha256,
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
                source_sha256_resolver=_source_sha256,
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
            source_sha256_resolver=_source_sha256,
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
            self.assertEqual(
                source_sha256,
                f"sha256:{hashlib.sha256(source_bytes).hexdigest()}",
            )
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


class AV1ValidationPartitionCliTests(unittest.TestCase):
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
