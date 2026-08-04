from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from mediaforce.tuning.av1_validation_v3 import load_av1_validation_protocol_v3
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationError,
    av1_validation_v3_qualification_attestation_from_payload,
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_coverage import (
    AV1ValidationV3Tier1CoverageError,
    assert_av1_validation_v3_tier1_coverage_attestation,
    av1_validation_v3_tier1_coverage_from_payload,
    build_av1_validation_v3_tier1_coverage_attestation,
    load_av1_validation_v3_tier1_coverage_attestation,
    serialize_av1_validation_v3_tier1_coverage_attestation,
)
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
    AV1ValidationV3Tier1FixtureOutcome,
)
from mediaforce.tuning.av1_validation_v3_tier1_grant import (
    build_av1_validation_v3_tier1_execution_grant,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    build_av1_validation_v3_tier1_authorization_request,
)


class AV1ValidationV3Tier1CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        protocol = load_av1_validation_protocol_v3(
            Path("docs/validation/av1-cold-start-preregistration-v3.json")
        )
        plan = build_av1_validation_v3_qualification_plan(
            protocol=protocol,
            qualification_key_id=f"av1vqkey3_{'a' * 32}",
            eligibility_predicate_sha256=f"sha256:{'b' * 64}",
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            config_sha256=f"sha256:{'c' * 64}",
            toolchain_sha256=f"sha256:{'d' * 64}",
            fixture_matrix_sha256=AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
            frozen_at="2026-08-04T12:00:00Z",
            valid_until="2026-08-05T12:00:00Z",
        )
        request = build_av1_validation_v3_tier1_authorization_request(
            protocol=protocol,
            plan=plan,
            requested_at="2026-08-04T13:00:00Z",
            valid_until="2026-08-04T20:00:00Z",
        )
        grant = build_av1_validation_v3_tier1_execution_grant(
            protocol=protocol,
            plan=plan,
            request=request,
            owner_principal="owner-1234abcd",
            authorized_at="2026-08-04T14:00:00Z",
            valid_until="2026-08-04T19:00:00Z",
        )
        self.protocol, self.plan, self.request, self.grant = protocol, plan, request, grant

    def test_coverage_is_gate_a0_only_and_public_summary_is_honest(self) -> None:
        attestation = self._build()
        payload = attestation.to_payload()
        self.assertEqual(payload["gate"], "A0")
        self.assertFalse(payload["qualification_complete"])
        self.assertFalse(payload["tier2_complete"])
        self.assertFalse(payload["evidence_eligible"])
        self.assertEqual(attestation.to_public_summary()["fixtures_passed"], 4)
        self.assertEqual(attestation.to_public_summary()["attestation_id"], attestation.attestation_id)

    def test_coverage_round_trips_canonical_bytes(self) -> None:
        attestation = self._build()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.json"
            path.write_bytes(serialize_av1_validation_v3_tier1_coverage_attestation(attestation))
            self.assertEqual(load_av1_validation_v3_tier1_coverage_attestation(path), attestation)

    def test_coverage_rejects_incomplete_fixture_set_and_cleanup_failure(self) -> None:
        with self.assertRaisesRegex(AV1ValidationV3Tier1CoverageError, "incomplete"):
            self._build(outcomes=self._outcomes()[:-1])
        with self.assertRaisesRegex(AV1ValidationV3Tier1CoverageError, "cleanup"):
            self._build(cleanup_passed=False)
        with self.assertRaisesRegex(AV1ValidationV3Tier1CoverageError, "paused runtime"):
            self._build(runtime_paused=False)

    def test_coverage_rejects_constant_field_tampering(self) -> None:
        for field, value in (("evidence_eligible", True), ("gate", "A")):
            with self.subTest(field=field):
                payload = self._build().to_payload()
                payload[field] = value
                with self.assertRaises(AV1ValidationV3Tier1CoverageError):
                    av1_validation_v3_tier1_coverage_from_payload(payload)

    def test_coverage_rejects_float_counts_and_noncanonical_timestamp(self) -> None:
        payload = self._build().to_payload()
        payload["fixtures"][0]["content_byte_count"] = 796_262_400.0
        with self.assertRaises(AV1ValidationV3Tier1CoverageError):
            av1_validation_v3_tier1_coverage_from_payload(payload)
        payload = self._build().to_payload()
        payload["completed_at"] = "2026-08-04T18:00:00+00:00"
        with self.assertRaisesRegex(AV1ValidationV3Tier1CoverageError, "canonical UTC"):
            av1_validation_v3_tier1_coverage_from_payload(payload)

    def test_assertion_revalidates_integrity_and_active_bindings(self) -> None:
        attestation = self._build()
        assert_av1_validation_v3_tier1_coverage_attestation(
            self.protocol, self.plan, self.request, self.grant, attestation
        )
        with self.assertRaises(AV1ValidationV3Tier1CoverageError):
            assert_av1_validation_v3_tier1_coverage_attestation(
                self.protocol,
                self.plan,
                self.request,
                self.grant,
                replace(attestation, repository_commit="3" * 40),
            )

    def test_coverage_cannot_be_loaded_as_full_qualification_attestation(self) -> None:
        with self.assertRaises(AV1ValidationV3QualificationError):
            av1_validation_v3_qualification_attestation_from_payload(
                self._build().to_payload()
            )

    def _build(
        self,
        *,
        outcomes: tuple[AV1ValidationV3Tier1FixtureOutcome, ...] | None = None,
        cleanup_passed: bool = True,
        runtime_paused: bool = True,
    ):
        return build_av1_validation_v3_tier1_coverage_attestation(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            grant=self.grant,
            outcomes=outcomes or self._outcomes(),
            cleanup_passed=cleanup_passed,
            runtime_paused=runtime_paused,
            completed_at="2026-08-04T18:00:00Z",
        )

    def _outcomes(self) -> tuple[AV1ValidationV3Tier1FixtureOutcome, ...]:
        return tuple(
            AV1ValidationV3Tier1FixtureOutcome(
                fixture_id=fixture_id,
                matrix_sha256=AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
                request_id=self.request.request_id,
                grant_id=self.grant.grant_id,
                repository_commit=self.plan.repository_commit,
                toolchain_sha256=self.plan.toolchain_sha256,
                command_plan_sha256=f"sha256:{index:064x}",
                content_sha256=f"sha256:{index + 10:064x}",
                content_byte_count=796_262_400,
                passed=True,
                failures=(),
                observation={},
            )
            for index, fixture_id in enumerate((
                "tier1_flat_field",
                "tier1_high_detail_noise",
                "tier1_high_motion",
                "tier1_scene_change",
            ))
        )
