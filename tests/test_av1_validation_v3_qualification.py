import copy
import json
from pathlib import Path
import tempfile
import unittest

from mediaforce.tuning.av1_validation_v3 import (
    AV1ValidationV3Error,
    assert_av1_validation_v3_evidence_artifact,
    load_av1_validation_protocol_v3,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationError,
    AV1ValidationV3QualificationPath,
    assert_av1_validation_v3_qualification_attestation,
    assert_av1_validation_v3_qualification_plan_active,
    av1_validation_v3_expected_path_matrix_sha256,
    av1_validation_v3_qualification_attestation_from_payload,
    av1_validation_v3_qualification_plan_from_payload,
    build_av1_validation_v3_expected_qualification_paths,
    build_av1_validation_v3_qualification_attestation,
    build_av1_validation_v3_qualification_plan,
    load_av1_validation_v3_qualification_attestation,
    load_av1_validation_v3_qualification_plan,
    serialize_av1_validation_v3_qualification_attestation,
    serialize_av1_validation_v3_qualification_plan,
)


V3_PROTOCOL_PATH = Path("docs/validation/av1-cold-start-preregistration-v3.json")
SHA256 = f"sha256:{'a' * 64}"
COMMIT = "1" * 40
TREE = "2" * 40
FROZEN_AT = "2026-08-03T12:00:00Z"
VALID_UNTIL = "2026-08-04T12:00:00Z"
ACCEPTED_AT = "2026-08-03T13:00:00Z"


class AV1ValidationV3QualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = load_av1_validation_protocol_v3(V3_PROTOCOL_PATH)
        self.plan = build_av1_validation_v3_qualification_plan(
            protocol=self.protocol,
            qualification_key_id=f"av1vqkey3_{'b' * 32}",
            eligibility_predicate_sha256=SHA256,
            repository_commit=COMMIT,
            repository_tree=TREE,
            config_sha256=SHA256,
            toolchain_sha256=SHA256,
            fixture_matrix_sha256=SHA256,
            frozen_at=FROZEN_AT,
            valid_until=VALID_UNTIL,
        )

    def test_plan_is_deterministic_canonical_and_nonexecuting(self) -> None:
        rebuilt = build_av1_validation_v3_qualification_plan(
            protocol=self.protocol,
            qualification_key_id=f"av1vqkey3_{'b' * 32}",
            eligibility_predicate_sha256=SHA256,
            repository_commit=COMMIT,
            repository_tree=TREE,
            config_sha256=SHA256,
            toolchain_sha256=SHA256,
            fixture_matrix_sha256=SHA256,
            frozen_at=FROZEN_AT,
            valid_until=VALID_UNTIL,
        )
        self.assertEqual(self.plan, rebuilt)
        self.assertEqual(
            self.plan.expected_path_matrix_sha256,
            av1_validation_v3_expected_path_matrix_sha256(self.protocol),
        )
        payload = self.plan.to_payload()
        self.assertFalse(payload["runtime_execution_authorized"])
        self.assertFalse(payload["private_inventory_read_authorized"])
        self.assertFalse(payload["qualification_execution_authorized"])
        self.assertFalse(payload["evidence_eligible"])
        self.assertEqual(payload["gate"], "A0")
        self.assertEqual(
            av1_validation_v3_qualification_plan_from_payload(payload),
            self.plan,
        )

    def test_expected_matrix_covers_tier_success_and_all_fault_paths(self) -> None:
        paths = build_av1_validation_v3_expected_qualification_paths(self.protocol)
        self.assertEqual(paths, tuple(sorted(paths, key=lambda path: path.matrix_key)))
        tier1_successes = [
            path
            for path in paths
            if path.tier == "tier1" and path.path_kind == "success"
        ]
        tier2_successes = [
            path
            for path in paths
            if path.tier == "tier2" and path.path_kind == "success"
        ]
        self.assertEqual(
            {path.candidate_configuration for path in tier1_successes},
            {cell.name for cell in self.protocol.candidate_cells},
        )
        self.assertEqual(len(tier2_successes), 4)
        self.assertTrue(any(path.path_kind == "runtime_failure" for path in paths))
        self.assertTrue(any(path.path_kind == "terminal" for path in paths))
        self.assertTrue(any(path.path_kind == "recovery" for path in paths))

    def test_attestation_requires_complete_bound_matrix_and_paused_cleanup(self) -> None:
        attestation = build_av1_validation_v3_qualification_attestation(
            protocol=self.protocol,
            plan=self.plan,
            paths=build_av1_validation_v3_expected_qualification_paths(self.protocol),
            cleanup_passed=True,
            runtime_paused=True,
            accepted_at=ACCEPTED_AT,
        )
        assert_av1_validation_v3_qualification_attestation(
            self.protocol,
            self.plan,
            attestation,
        )
        self.assertEqual(
            attestation.to_public_summary(),
            {
                "qualification_valid": True,
                "tier1_complete": True,
                "tier2_complete": True,
                "total_path_count": len(attestation.paths),
                "runtime_execution_authorized": False,
                "evidence_eligible": False,
            },
        )
        with self.assertRaises(AV1ValidationV3Error):
            assert_av1_validation_v3_evidence_artifact(attestation.to_payload())

        with self.assertRaisesRegex(AV1ValidationV3QualificationError, "incomplete"):
            build_av1_validation_v3_qualification_attestation(
                protocol=self.protocol,
                plan=self.plan,
                paths=attestation.paths[1:],
                cleanup_passed=True,
                runtime_paused=True,
                accepted_at=ACCEPTED_AT,
            )
        with self.assertRaisesRegex(AV1ValidationV3QualificationError, "cleanup"):
            build_av1_validation_v3_qualification_attestation(
                protocol=self.protocol,
                plan=self.plan,
                paths=attestation.paths,
                cleanup_passed=False,
                runtime_paused=True,
                accepted_at=ACCEPTED_AT,
            )
        with self.assertRaisesRegex(AV1ValidationV3QualificationError, "not active"):
            build_av1_validation_v3_qualification_attestation(
                protocol=self.protocol,
                plan=self.plan,
                paths=attestation.paths,
                cleanup_passed=True,
                runtime_paused=True,
                accepted_at=VALID_UNTIL,
            )

    def test_active_checks_fail_closed_before_freeze_at_expiry_and_after_protocol_expiry(self) -> None:
        for checked_at in (
            "2026-08-03T11:59:59Z",
            VALID_UNTIL,
            "2027-01-29T16:52:28Z",
        ):
            with self.subTest(checked_at=checked_at):
                with self.assertRaises(AV1ValidationV3QualificationError):
                    assert_av1_validation_v3_qualification_plan_active(
                        self.protocol,
                        self.plan,
                        as_of=checked_at,
                    )

    def test_path_shapes_reject_private_fault_coverage_and_incomplete_success_binding(self) -> None:
        with self.assertRaisesRegex(AV1ValidationV3QualificationError, "non-private Tier 1"):
            AV1ValidationV3QualificationPath(
                tier="tier2",
                path_kind="runtime_failure",
                path_name="runtime_preflight_failure",
                candidate_configuration=None,
                stratum_id=None,
                passed=True,
            )
        with self.assertRaisesRegex(AV1ValidationV3QualificationError, "requires a private stratum"):
            AV1ValidationV3QualificationPath(
                tier="tier2",
                path_kind="success",
                path_name="complete_success",
                candidate_configuration="darkness_balanced_candidate",
                stratum_id=None,
                passed=True,
            )

    def test_serialized_plan_and_attestation_require_canonical_exact_contracts(self) -> None:
        attestation = build_av1_validation_v3_qualification_attestation(
            protocol=self.protocol,
            plan=self.plan,
            paths=build_av1_validation_v3_expected_qualification_paths(self.protocol),
            cleanup_passed=True,
            runtime_paused=True,
            accepted_at=ACCEPTED_AT,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            attestation_path = root / "attestation.json"
            plan_path.write_bytes(serialize_av1_validation_v3_qualification_plan(self.plan))
            attestation_path.write_bytes(
                serialize_av1_validation_v3_qualification_attestation(attestation)
            )
            self.assertEqual(load_av1_validation_v3_qualification_plan(plan_path), self.plan)
            self.assertEqual(
                load_av1_validation_v3_qualification_attestation(attestation_path),
                attestation,
            )

            mutated = copy.deepcopy(self.plan.to_payload())
            mutated["qualification_execution_authorized"] = True
            plan_path.write_text(json.dumps(mutated), encoding="utf-8")
            with self.assertRaises(AV1ValidationV3QualificationError):
                load_av1_validation_v3_qualification_plan(plan_path)

    def test_attestation_cannot_be_rebound_to_a_different_plan(self) -> None:
        attestation = build_av1_validation_v3_qualification_attestation(
            protocol=self.protocol,
            plan=self.plan,
            paths=build_av1_validation_v3_expected_qualification_paths(self.protocol),
            cleanup_passed=True,
            runtime_paused=True,
            accepted_at=ACCEPTED_AT,
        )
        different_plan = build_av1_validation_v3_qualification_plan(
            protocol=self.protocol,
            qualification_key_id=f"av1vqkey3_{'c' * 32}",
            eligibility_predicate_sha256=SHA256,
            repository_commit=COMMIT,
            repository_tree=TREE,
            config_sha256=SHA256,
            toolchain_sha256=SHA256,
            fixture_matrix_sha256=SHA256,
            frozen_at=FROZEN_AT,
            valid_until=VALID_UNTIL,
        )
        with self.assertRaisesRegex(AV1ValidationV3QualificationError, "bound"):
            assert_av1_validation_v3_qualification_attestation(
                self.protocol,
                different_plan,
                attestation,
            )

    def test_payload_parser_rejects_tampered_attestation_gate(self) -> None:
        attestation = build_av1_validation_v3_qualification_attestation(
            protocol=self.protocol,
            plan=self.plan,
            paths=build_av1_validation_v3_expected_qualification_paths(self.protocol),
            cleanup_passed=True,
            runtime_paused=True,
            accepted_at=ACCEPTED_AT,
        )
        payload = copy.deepcopy(attestation.to_payload())
        payload["gate"] = "B"
        with self.assertRaises(AV1ValidationV3QualificationError):
            av1_validation_v3_qualification_attestation_from_payload(payload)
