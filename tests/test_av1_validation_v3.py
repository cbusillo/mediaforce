import copy
import json
from pathlib import Path
import tempfile
import unittest

from mediaforce.tuning.av1_validation_derivation import (
    AV1_VALIDATION_DERIVATION_REASON_CODES,
)
from mediaforce.tuning.av1_validation_v2 import load_av1_validation_manifest_v2
from mediaforce.tuning.av1_validation_v3 import (
    AV1_VALIDATION_V3_DERIVATION_RESERVE_COUNT,
    AV1_VALIDATION_V3_EXPERIMENT_ID,
    AV1_VALIDATION_V3_GLOBAL_STOP_THRESHOLD,
    AV1_VALIDATION_V3_HOLDOUT_COUNT,
    AV1_VALIDATION_V3_QUALIFICATION_AUTHORITY,
    AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
    AV1_VALIDATION_V3_RANGE_HIT_MINIMUM,
    AV1_VALIDATION_V3_REQUIRED_DERIVATION_OBSERVATIONS,
    AV1_VALIDATION_V3_SIGN_TEST_MINIMUM_WINS,
    AV1_VALIDATION_V3_SUPERSEDES_MANIFEST_ID,
    AV1_VALIDATION_V3_SUPERSEDES_PAYLOAD_SHA256,
    AV1_VALIDATION_V3_VOID_CAP_PER_CELL,
    AV1ValidationV3Chronology,
    AV1ValidationV3Error,
    AV1ValidationV3QualificationSource,
    assert_av1_validation_v3_artifact_namespace,
    assert_av1_validation_v3_evidence_artifact,
    assert_av1_validation_v3_evidence_marker,
    assert_av1_validation_v3_protocol_active,
    assert_preregistered_av1_validation_protocol_v3,
    av1_validation_protocol_v3_from_payload,
    av1_validation_v3_global_stop_required,
    av1_validation_v3_hmac_domain,
    av1_validation_v3_qualification_key_id,
    av1_validation_v3_terminal_disposition,
    build_preregistered_av1_validation_protocol_v3,
    load_av1_validation_protocol_v3,
    select_av1_validation_v3_tier2_sources,
    serialize_av1_validation_protocol_v3,
)


V2_PROTOCOL_PATH = Path("docs/validation/av1-cold-start-preregistration-v2.json")
V3_PROTOCOL_PATH = Path("docs/validation/av1-cold-start-preregistration-v3.json")


class AV1ValidationV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = load_av1_validation_protocol_v3(V3_PROTOCOL_PATH)

    def test_checked_in_protocol_is_canonical_and_v2_is_unchanged(self) -> None:
        expected = build_preregistered_av1_validation_protocol_v3()
        self.assertEqual(self.protocol, expected)
        self.assertEqual(
            V3_PROTOCOL_PATH.read_bytes(),
            serialize_av1_validation_protocol_v3(expected),
        )
        self.assertEqual(
            self.protocol.protocol_id,
            "av1vprotocol3_ba85a44eef70b857d678b236bb1b4afc",
        )
        self.assertEqual(
            self.protocol.payload_sha256,
            "sha256:d17606e4920846de810ab467d63a194f6a9b9138f6d8416a3ff3e0416c37a590",
        )
        assert_preregistered_av1_validation_protocol_v3(self.protocol)

        v2 = load_av1_validation_manifest_v2(V2_PROTOCOL_PATH)
        self.assertEqual(
            v2.payload_sha256,
            "sha256:5a24bbfdfe699aa2c6f037b9473ce06607aee20af743f57266f38bf7eb08d268",
        )

    def test_protocol_freezes_owner_approved_nonexecuting_values(self) -> None:
        payload = self.protocol.to_payload()
        self.assertEqual(payload["protocol_version"], 3)
        self.assertEqual(payload["experiment_id"], AV1_VALIDATION_V3_EXPERIMENT_ID)
        self.assertFalse(payload["runtime_execution_authorized"])
        self.assertFalse(payload["qualification_execution_authorized"])
        self.assertFalse(payload["private_inventory_read_authorized"])
        self.assertFalse(payload["key_creation_authorized"])
        self.assertFalse(payload["partition_construction_authorized"])
        self.assertFalse(payload["derivation_execution_authorized"])
        self.assertFalse(payload["holdout_execution_authorized"])
        self.assertFalse(payload["public_bundle_activation_allowed"])

        partition = payload["partition_policy"]
        self.assertEqual(
            partition["required_derivation_observations_per_cell"],
            AV1_VALIDATION_V3_REQUIRED_DERIVATION_OBSERVATIONS,
        )
        self.assertEqual(
            partition["derivation_reserve_count_per_cell"],
            AV1_VALIDATION_V3_DERIVATION_RESERVE_COUNT,
        )
        self.assertEqual(
            partition["void_cap_per_cell"],
            AV1_VALIDATION_V3_VOID_CAP_PER_CELL,
        )
        self.assertEqual(
            partition["global_same_stage_stop_threshold"],
            AV1_VALIDATION_V3_GLOBAL_STOP_THRESHOLD,
        )
        self.assertEqual(partition["holdout_count_per_cell"], AV1_VALIDATION_V3_HOLDOUT_COUNT)

        holdout = payload["holdout_policy"]
        self.assertEqual(holdout["range_hit_minimum"], AV1_VALIDATION_V3_RANGE_HIT_MINIMUM)
        self.assertTrue(holdout["tests_form_intersection_union"])
        self.assertTrue(holdout["component_power_only"])
        self.assertFalse(holdout["joint_power_claim_allowed"])

    def test_protocol_rejects_mutated_execution_flags(self) -> None:
        for flag in (
            "runtime_execution_authorized",
            "qualification_execution_authorized",
            "private_inventory_read_authorized",
            "key_creation_authorized",
            "partition_construction_authorized",
            "derivation_execution_authorized",
            "holdout_execution_authorized",
            "public_bundle_activation_allowed",
        ):
            with self.subTest(flag=flag):
                payload = json.loads(V3_PROTOCOL_PATH.read_bytes())
                payload[flag] = True
                with self.assertRaises(AV1ValidationV3Error):
                    av1_validation_protocol_v3_from_payload(payload)

    def test_v3_supersession_link_pins_v2_manifest_exactly(self) -> None:
        self.assertEqual(
            self.protocol.supersedes_manifest_id,
            AV1_VALIDATION_V3_SUPERSEDES_MANIFEST_ID,
        )
        self.assertEqual(
            self.protocol.supersedes_payload_sha256,
            AV1_VALIDATION_V3_SUPERSEDES_PAYLOAD_SHA256,
        )
        v2 = load_av1_validation_manifest_v2(V2_PROTOCOL_PATH)
        self.assertEqual(self.protocol.supersedes_manifest_id, v2.manifest_id)
        self.assertEqual(self.protocol.supersedes_payload_sha256, v2.payload_sha256)

    def test_protocol_power_table_matches_frozen_component_values(self) -> None:
        actual = {
            point.true_rate: point.component_power
            for point in self.protocol.power_table
        }
        expected = {
            0.50: 0.010635376,
            0.60: 0.065146742,
            0.70: 0.245855864,
            0.75: 0.404987110,
            0.80: 0.598134326,
            0.85: 0.789890703,
            0.90: 0.931593826,
            0.95: 0.992996092,
            1.00: 1.000000000,
        }
        self.assertEqual(actual, expected)

    def test_tier2_selection_is_keyed_canonical_and_private(self) -> None:
        qualification_key = b"qualification-key" * 2
        key_id = av1_validation_v3_qualification_key_id(qualification_key)
        sources = (
            self._source(1, "animation"),
            self._source(2, "animation"),
            self._source(3, "typical"),
            self._source(4, "typical"),
        )
        forward = select_av1_validation_v3_tier2_sources(
            protocol=self.protocol,
            sources=sources,
            qualification_key=qualification_key,
            expected_key_id=key_id,
        )
        reverse = select_av1_validation_v3_tier2_sources(
            protocol=self.protocol,
            sources=tuple(reversed(sources)),
            qualification_key=qualification_key,
            expected_key_id=key_id,
        )
        self.assertEqual(forward, reverse)
        self.assertEqual(
            {selection.stratum_name for selection in forward},
            {
                "animation_balanced_qualification",
                "typical_balanced_qualification",
            },
        )
        for selection in forward:
            public = selection.to_public_payload()
            self.assertNotIn("source_fingerprint", public)
            self.assertNotIn("rank_sha256", public)
            self.assertFalse(public["runtime_execution_authorized"])
            self.assertFalse(public["evidence_eligible"])

    def test_tier2_selection_fails_closed(self) -> None:
        qualification_key = b"qualification-key" * 2
        key_id = av1_validation_v3_qualification_key_id(qualification_key)
        minimal = select_av1_validation_v3_tier2_sources(
            protocol=self.protocol,
            sources=(self._source(1, "animation"), self._source(2, "typical")),
            qualification_key=qualification_key,
            expected_key_id=key_id,
        )
        self.assertEqual(len(minimal), 2)
        with self.assertRaises(AV1ValidationV3Error):
            select_av1_validation_v3_tier2_sources(
                protocol=self.protocol,
                sources=(),
                qualification_key=qualification_key,
                expected_key_id=key_id,
            )
        with self.assertRaises(AV1ValidationV3Error):
            select_av1_validation_v3_tier2_sources(
                protocol=self.protocol,
                sources=(self._source(1, "animation"),),
                qualification_key=qualification_key,
                expected_key_id=key_id,
            )
        duplicate_fingerprint = f"sha256:{1:064x}"
        with self.assertRaises(AV1ValidationV3Error):
            select_av1_validation_v3_tier2_sources(
                protocol=self.protocol,
                sources=(
                    AV1ValidationV3QualificationSource(
                        source_fingerprint=duplicate_fingerprint,
                        intent_level="balanced",
                        exact_traits=("animation",),
                        pipeline_ready=True,
                    ),
                    AV1ValidationV3QualificationSource(
                        source_fingerprint=duplicate_fingerprint,
                        intent_level="balanced",
                        exact_traits=("typical",),
                        pipeline_ready=True,
                    ),
                ),
                qualification_key=qualification_key,
                expected_key_id=key_id,
            )
        with self.assertRaises(AV1ValidationV3Error):
            select_av1_validation_v3_tier2_sources(
                protocol=self.protocol,
                sources=(
                    self._source(1, "animation", pipeline_ready=False),
                    self._source(2, "typical"),
                ),
                qualification_key=qualification_key,
                expected_key_id=key_id,
            )
        with self.assertRaises(AV1ValidationV3Error):
            select_av1_validation_v3_tier2_sources(
                protocol=self.protocol,
                sources=(self._source(1, "animation"), self._source(2, "typical")),
                qualification_key=qualification_key,
                expected_key_id=av1_validation_v3_qualification_key_id(b"other-key" * 4),
            )
        with self.assertRaises(AV1ValidationV3Error):
            select_av1_validation_v3_tier2_sources(
                protocol=self.protocol,
                sources=(
                    self._source(1, "animation"),
                    self._source(2, "typical"),
                    self._source(3, "darkness"),
                ),
                qualification_key=qualification_key,
                expected_key_id=key_id,
            )

    def test_qualification_key_requires_32_bytes(self) -> None:
        with self.assertRaises(AV1ValidationV3Error):
            av1_validation_v3_qualification_key_id(b"short")
        with self.assertRaises(AV1ValidationV3Error):
            select_av1_validation_v3_tier2_sources(
                protocol=self.protocol,
                sources=(self._source(1, "animation"), self._source(2, "typical")),
                qualification_key=b"short",
                expected_key_id=f"av1vqkey3_{'a' * 32}",
            )

    def test_qualification_and_evidence_barriers_are_independent(self) -> None:
        qualification = {
            "protocol_version": 3,
            "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
            "authority": AV1_VALIDATION_V3_QUALIFICATION_AUTHORITY,
            "artifact_namespace": AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
            "evidence_eligible": False,
        }
        assert_av1_validation_v3_artifact_namespace(
            qualification,
            evidence_required=False,
        )
        assert_av1_validation_v3_evidence_marker(
            qualification,
            evidence_required=False,
        )
        with self.assertRaises(AV1ValidationV3Error):
            assert_av1_validation_v3_artifact_namespace(
                {**qualification, "evidence_eligible": True},
                evidence_required=True,
            )

        empirical = {
            "protocol_version": 3,
            "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
            "authority": "av1_v3_derivation",
            "artifact_namespace": "av1_v3_empirical",
            "evidence_eligible": True,
        }
        assert_av1_validation_v3_evidence_artifact(empirical)
        with self.assertRaises(AV1ValidationV3Error):
            assert_av1_validation_v3_artifact_namespace(
                empirical,
                evidence_required=False,
            )
        with self.assertRaises(AV1ValidationV3Error):
            assert_av1_validation_v3_evidence_marker(
                {**empirical, "evidence_eligible": False},
                evidence_required=True,
            )
        with self.assertRaises(AV1ValidationV3Error):
            assert_av1_validation_v3_evidence_marker(
                {key: value for key, value in empirical.items() if key != "evidence_eligible"},
                evidence_required=True,
            )

    def test_chronology_is_machine_enforced(self) -> None:
        chronology = AV1ValidationV3Chronology(
            predicate_frozen_at="2026-08-03T00:00:00Z",
            qualification_key_committed_at="2026-08-03T00:00:00Z",
            tier2_selected_at="2026-08-03T00:01:00Z",
            qualification_accepted_at="2026-08-03T01:00:00Z",
            empirical_key_committed_at="2026-08-03T01:01:00Z",
            eligibility_attestation_cutoff="2026-08-03T01:02:00Z",
            partition_selected_at="2026-08-03T01:02:00Z",
            valid_until="2026-08-04T00:00:00Z",
        )
        self.assertEqual(
            chronology.to_payload()["empirical_key_committed_at"],
            "2026-08-03T01:01:00Z",
        )
        self.assertEqual(
            chronology.predicate_frozen_at,
            chronology.qualification_key_committed_at,
        )
        invalid = copy.copy(chronology.to_payload())
        invalid["empirical_key_committed_at"] = "2026-08-03T00:30:00Z"
        with self.assertRaises(AV1ValidationV3Error):
            AV1ValidationV3Chronology(**invalid)

    def test_protocol_activity_is_explicit_and_time_bounded(self) -> None:
        assert_av1_validation_v3_protocol_active(
            self.protocol,
            as_of="2026-08-02T16:52:28Z",
        )
        assert_av1_validation_v3_protocol_active(
            self.protocol,
            as_of="2027-01-29T16:52:27Z",
        )
        with self.assertRaises(AV1ValidationV3Error):
            assert_av1_validation_v3_protocol_active(
                self.protocol,
                as_of="2026-08-02T16:52:27Z",
            )
        with self.assertRaises(AV1ValidationV3Error):
            assert_av1_validation_v3_protocol_active(
                self.protocol,
                as_of="2027-01-29T16:52:28Z",
            )

    def test_terminal_matrix_is_total_and_fail_closed(self) -> None:
        for reason_code in AV1_VALIDATION_DERIVATION_REASON_CODES:
            disposition = av1_validation_v3_terminal_disposition(
                reason_code,
                machine_emitted=True,
                content_measurement_started=False,
                review_media_exists=False,
            )
            self.assertIn(
                disposition,
                {"assignment_void", "cell_terminal", "protocol_nonconformance"},
            )
        self.assertEqual(
            av1_validation_v3_terminal_disposition(
                "runtime_preflight_failure",
                machine_emitted=True,
                content_measurement_started=False,
                review_media_exists=False,
            ),
            "assignment_void",
        )
        self.assertEqual(
            av1_validation_v3_terminal_disposition(
                "runtime_preflight_failure",
                machine_emitted=True,
                content_measurement_started=True,
                review_media_exists=False,
            ),
            "cell_terminal",
        )
        for reason_code in ("authorization_expired", "interrupted_claim"):
            with self.subTest(reason_code=reason_code):
                self.assertEqual(
                    av1_validation_v3_terminal_disposition(
                        reason_code,
                        machine_emitted=True,
                        content_measurement_started=False,
                        review_media_exists=False,
                    ),
                    "assignment_void",
                )
        self.assertEqual(
            av1_validation_v3_terminal_disposition(
                "authorization_expired",
                machine_emitted=False,
                content_measurement_started=False,
                review_media_exists=False,
            ),
            "cell_terminal",
        )
        self.assertEqual(
            av1_validation_v3_terminal_disposition(
                "runtime_failure",
                machine_emitted=True,
                content_measurement_started=False,
                review_media_exists=False,
            ),
            "protocol_nonconformance",
        )
        self.assertEqual(
            av1_validation_v3_terminal_disposition(
                "future_unknown_reason",
                machine_emitted=True,
                content_measurement_started=False,
                review_media_exists=False,
            ),
            "cell_terminal",
        )

    def test_global_stop_uses_frozen_same_stage_threshold(self) -> None:
        self.assertFalse(av1_validation_v3_global_stop_required(
            reason_code="runtime_source_snapshot_failure",
            global_void_count=2,
        ))
        self.assertTrue(av1_validation_v3_global_stop_required(
            reason_code="runtime_source_snapshot_failure",
            global_void_count=3,
        ))
        with self.assertRaises(AV1ValidationV3Error):
            av1_validation_v3_global_stop_required(
                reason_code="runtime_quality_search_failure",
                global_void_count=3,
            )

    def test_protocol_domains_are_v3_specific(self) -> None:
        domain = av1_validation_v3_hmac_domain("tier2:qualification-rank")
        self.assertIn(":3:", domain)
        self.assertIn(AV1_VALIDATION_V3_EXPERIMENT_ID, domain)
        self.assertNotEqual(domain, "av1vtok1:selection-rank")
        self.assertTrue(
            av1_validation_v3_qualification_key_id(b"qualification-key" * 2).startswith(
                "av1vqkey3_"
            )
        )

    def test_range_hit_minimum_matches_sign_test_at_holdout_count(self) -> None:
        self.assertEqual(
            AV1_VALIDATION_V3_SIGN_TEST_MINIMUM_WINS[
                AV1_VALIDATION_V3_HOLDOUT_COUNT
            ],
            AV1_VALIDATION_V3_RANGE_HIT_MINIMUM,
        )
        self.assertEqual(
            AV1_VALIDATION_V3_SIGN_TEST_MINIMUM_WINS[:6],
            (None, None, None, None, None, None),
        )

    def test_loader_rejects_noncanonical_or_normalized_payloads(self) -> None:
        payload = json.loads(V3_PROTOCOL_PATH.read_bytes())
        changed = copy.deepcopy(payload)
        changed["tier2_qualification"]["ranking_algorithm"] = "manual_selection"
        with self.assertRaises(AV1ValidationV3Error):
            av1_validation_protocol_v3_from_payload(changed)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protocol.json"
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            with self.assertRaises(AV1ValidationV3Error):
                load_av1_validation_protocol_v3(path)

    def test_preregistration_verifier_recognizes_v3_without_execution_authority(self) -> None:
        from scripts import verify_av1_cold_start_preregistration

        loaded = verify_av1_cold_start_preregistration._load_manifest(V3_PROTOCOL_PATH)
        self.assertEqual(loaded, self.protocol)
        payload = verify_av1_cold_start_preregistration._validation_payload(loaded)
        self.assertEqual(payload["protocol_version"], 3)
        self.assertEqual(payload["candidate_cell_count"], 2)
        self.assertEqual(payload["tier2_stratum_count"], 2)
        self.assertFalse(payload["runtime_execution_authorized"])
        self.assertFalse(payload["qualification_execution_authorized"])
        self.assertFalse(payload["private_inventory_read_authorized"])
        self.assertFalse(payload["key_creation_authorized"])
        self.assertFalse(payload["partition_construction_authorized"])
        self.assertFalse(payload["derivation_execution_authorized"])
        self.assertFalse(payload["holdout_execution_authorized"])

    @staticmethod
    def _source(
        ordinal: int,
        trait: str,
        *,
        pipeline_ready: bool = True,
    ) -> AV1ValidationV3QualificationSource:
        return AV1ValidationV3QualificationSource(
            source_fingerprint=f"sha256:{ordinal:064x}",
            intent_level="balanced",
            exact_traits=(trait,),
            pipeline_ready=pipeline_ready,
        )


if __name__ == "__main__":
    unittest.main()
