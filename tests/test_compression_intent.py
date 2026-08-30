import unittest

from mediaforce.tuning.compression_intent import (
    CompressionEvidenceRef,
    CompressionIntentV1,
    authorize_compression_change,
    compression_candidate_rank,
    compression_intent_from_policy,
    compression_intent_from_request,
    compression_intent_options,
)


class CompressionIntentTests(unittest.TestCase):
    def test_missing_policy_is_legacy_unconfirmed(self) -> None:
        intent = compression_intent_from_policy({})

        self.assertEqual(intent.level, "legacy_unconfirmed")
        self.assertTrue(intent.requires_confirmation)

    def test_unversioned_or_unconfirmed_named_policy_is_not_authoritative(self) -> None:
        unversioned = compression_intent_from_policy(
            {"compression_intent": "balanced", "compression_intent_confirmed": True}
        )
        unconfirmed = compression_intent_from_policy(
            {"compression_intent_schema_version": 1, "compression_intent": "balanced"}
        )

        self.assertEqual(unversioned.level, "legacy_unconfirmed")
        self.assertEqual(unconfirmed.level, "balanced")
        self.assertTrue(unconfirmed.requires_confirmation)

    def test_unconfirmed_intent_does_not_persist_a_policy_sentinel(self) -> None:
        intent = CompressionIntentV1("legacy_unconfirmed", "legacy", False)

        self.assertEqual(intent.policy_fragment(), {"video": {}})

    def test_policy_round_trip_has_stable_identity(self) -> None:
        intent = compression_intent_from_policy(
            {
                "compression_intent_schema_version": 1,
                "compression_intent": "perceptual_floor",
                "compression_intent_source": "folder_override",
                "compression_intent_confirmed": True,
            }
        )

        self.assertEqual(intent.level, "perceptual_floor")
        self.assertFalse(intent.requires_confirmation)
        self.assertEqual(intent.semantic_id, CompressionIntentV1("perceptual_floor", "other", True).semantic_id)
        self.assertNotEqual(intent.snapshot_id, CompressionIntentV1("perceptual_floor", "other", True).snapshot_id)

    def test_operator_request_rejects_legacy_level(self) -> None:
        with self.assertRaisesRegex(ValueError, "supported compression goal"):
            compression_intent_from_request(
                {"schema_version": 1, "level": "legacy_unconfirmed", "confirmed": True}
            )

    def test_options_mark_the_retained_intent(self) -> None:
        intent = CompressionIntentV1("transparent", "folder_override", True)

        options = compression_intent_options(intent)

        self.assertEqual(
            [option["key"] for option in options],
            ["reference", "balanced", "perceptual_floor"],
        )
        self.assertEqual([option["key"] for option in options if option["selected"]], ["perceptual_floor"])
        self.assertEqual(options[2]["compression_intent"]["level"], "perceptual_floor")
        self.assertEqual(
            {option["key"]: option["accepts_under_target_result"] for option in options},
            {
                "reference": False,
                "balanced": False,
                "perceptual_floor": True,
            },
        )
        self.assertIn("measured quality floor", options[2]["detail"])

    def test_legacy_options_do_not_preselect_a_named_intent(self) -> None:
        options = compression_intent_options(CompressionIntentV1("legacy_unconfirmed", "legacy", False))

        self.assertFalse(any(option["selected"] for option in options))

    def test_operator_request_requires_current_schema_and_confirmation(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported schema version"):
            compression_intent_from_request(
                {"schema_version": 2, "level": "transparent", "confirmed": True}
            )
        with self.assertRaisesRegex(ValueError, "Confirm the compression goal"):
            compression_intent_from_request(
                {"schema_version": 1, "level": "transparent", "confirmed": False}
            )
        with self.assertRaisesRegex(ValueError, "Confirm the compression goal"):
            compression_intent_from_request({"schema_version": 1, "level": "transparent"})

    def test_larger_result_requires_compatible_typed_evidence(self) -> None:
        intent = CompressionIntentV1("perceptual_floor", "operator", True)

        missing = authorize_compression_change(
            intent,
            authoritative_anchor_bytes=130_000_000,
            candidate_bytes=150_000_000,
        )
        incompatible = authorize_compression_change(
            intent,
            authoritative_anchor_bytes=130_000_000,
            candidate_bytes=150_000_000,
            evidence=(
                CompressionEvidenceRef(
                    kind="rejected_visual_result",
                    evidence_id="ev1",
                    intent_id=intent.semantic_id,
                    source_id="different",
                ),
            ),
            source_id="episode",
        )

        self.assertEqual(missing.outcome, "measure_more")
        self.assertEqual(incompatible.outcome, "measure_more")

    def test_rejection_can_authorize_item_local_growth(self) -> None:
        intent = CompressionIntentV1("perceptual_floor", "operator", True)
        evidence = CompressionEvidenceRef(
            kind="rejected_visual_result",
            evidence_id="ev1",
            intent_id=intent.semantic_id,
            observed_bytes=130_000_000,
            source_id="episode",
            policy_hash="policy",
            job_id="sample",
        )

        decision = authorize_compression_change(
            intent,
            authoritative_anchor_bytes=130_000_000,
            candidate_bytes=150_000_000,
            evidence=(evidence,),
            source_id="episode",
            policy_hash="policy",
            job_id="sample",
        )

        self.assertEqual(decision.outcome, "authorized")
        self.assertEqual(decision.escalation_scope, "item")
        self.assertEqual(decision.evidence_ids, ("ev1",))

    def test_job_bound_evidence_is_not_compatible_with_an_unbound_decision(self) -> None:
        intent = CompressionIntentV1("perceptual_floor", "operator", True)

        decision = authorize_compression_change(
            intent,
            authoritative_anchor_bytes=130_000_000,
            candidate_bytes=150_000_000,
            evidence=(
                CompressionEvidenceRef(
                    kind="rejected_visual_result",
                    evidence_id="ev1",
                    intent_id=intent.semantic_id,
                    job_id="stale-job",
                ),
            ),
        )

        self.assertEqual(decision.outcome, "measure_more")

    def test_growth_evidence_requires_all_scope_identities(self) -> None:
        intent = CompressionIntentV1("balanced", "operator", True)

        decision = authorize_compression_change(
            intent,
            authoritative_anchor_bytes=130_000_000,
            candidate_bytes=150_000_000,
            evidence=(
                CompressionEvidenceRef(
                    kind="measured_item_variance",
                    evidence_id="ev1",
                    intent_id=intent.semantic_id,
                ),
            ),
        )

        self.assertEqual(decision.outcome, "measure_more")

    def test_approval_never_authorizes_growth(self) -> None:
        intent = CompressionIntentV1("perceptual_floor", "operator", True)

        decision = authorize_compression_change(
            intent,
            authoritative_anchor_bytes=130_000_000,
            candidate_bytes=150_000_000,
            evidence=(
                CompressionEvidenceRef(
                    kind="approved_visual_result",
                    evidence_id="ev1",
                    intent_id=intent.semantic_id,
                ),
            ),
        )

        self.assertEqual(decision.outcome, "measure_more")

    def test_perceptual_floor_ranks_130_mb_before_150_mb(self) -> None:
        intent = CompressionIntentV1("perceptual_floor", "operator", True)
        smaller = compression_candidate_rank(
            intent,
            target_distance_bytes=10_000_000,
            predicted_bytes=130_000_000,
            metric_score=86.0,
            crf=31,
        )
        larger = compression_candidate_rank(
            intent,
            target_distance_bytes=10_000_000,
            predicted_bytes=150_000_000,
            metric_score=90.0,
            crf=29,
        )

        self.assertLess(smaller, larger)

    def test_unconfirmed_named_intent_uses_neutral_target_ordering(self) -> None:
        intent = CompressionIntentV1("reference", "legacy", False)
        closer = compression_candidate_rank(
            intent,
            target_distance_bytes=1_000_000,
            predicted_bytes=130_000_000,
            metric_score=86.0,
            crf=31,
        )
        higher_quality = compression_candidate_rank(
            intent,
            target_distance_bytes=10_000_000,
            predicted_bytes=150_000_000,
            metric_score=90.0,
            crf=29,
        )

        self.assertLess(closer, higher_quality)


if __name__ == "__main__":
    unittest.main()
