from dataclasses import replace
import json
from pathlib import Path
import tempfile
from typing import Any, Sequence
import unittest

from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1ColdStartValidationArmResultV1,
    AV1ColdStartValidationCandidateLockV1,
    AV1ColdStartValidationCaseV1,
    AV1ColdStartValidationCellPlanV1,
    AV1ColdStartValidationCellReportV1,
    AV1ColdStartValidationEvidenceSetV1,
    AV1ColdStartValidationError,
    AV1ColdStartValidationExecutionAuthorizationV1,
    AV1ColdStartValidationManifestV1,
    AV1ColdStartValidationPredictionStatus,
    AV1ColdStartValidationReportV1,
    AV1ColdStartValidationResultV1,
    av1_cold_start_validation_manifest_from_payload,
    assert_preregistered_av1_cold_start_validation_manifest,
    build_av1_cold_start_validation_candidate_lock,
    build_av1_cold_start_validation_evidence_set,
    build_av1_cold_start_validation_execution_authorization,
    build_av1_cold_start_validation_manifest,
    build_av1_cold_start_validation_report,
    build_av1_cold_start_validation_result,
    build_preregistered_av1_cold_start_validation_manifest,
    load_av1_cold_start_validation_evidence_set,
    load_av1_cold_start_validation_manifest,
    serialize_av1_cold_start_validation_evidence_set,
    serialize_av1_cold_start_validation_manifest,
)
from mediaforce.tuning.compression_intent import CompressionIntentLevel


ROOT = Path(__file__).resolve().parents[1]
PREREGISTRATION_PATH = ROOT / "docs" / "validation" / "av1-cold-start-preregistration-v1.json"


class AV1ColdStartEvaluationTests(unittest.TestCase):
    def test_checked_preregistration_is_canonical_deterministic_and_paused(self) -> None:
        manifest = self._manifest()
        checked = load_av1_cold_start_validation_manifest(PREREGISTRATION_PATH)

        self.assertEqual(checked, manifest)
        self.assertEqual(PREREGISTRATION_PATH.read_bytes(), serialize_av1_cold_start_validation_manifest(manifest))
        self.assertEqual(len(manifest.cell_plans), 12)
        self.assertEqual(len(manifest.cases), 62)
        self.assertIn("runtime_paused", manifest.execution_policy)
        payload = json.dumps(manifest.to_payload(), sort_keys=True)
        for forbidden in ("/Users/", "/Volumes/", "rel_path", "source_id", "review_media"):
            self.assertNotIn(forbidden, payload)

    def test_paused_manifest_report_does_not_synthesize_results(self) -> None:
        manifest = self._manifest()
        report = self._report(manifest, None)

        self.assertEqual(report.evidence_state, "not_started")
        self.assertEqual(report.observed_case_count, 0)
        self.assertFalse(report.supports_publication_review)
        self.assertEqual(
            report.blocking_reasons,
            ("evidence_incomplete", "no_publication_candidate_supported", "runtime_paused"),
        )
        self.assertTrue(all(cell.decision == "not_ready" for cell in report.cell_reports))

    def test_complete_candidate_uses_strict_preregistered_gates(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(manifest, plan.cell_plan_id, ("animation",))
        results = self._candidate_results(manifest, plan.cell_plan_id, candidate_lock.candidate_lock_id)
        evidence = self._evidence(manifest, [candidate_lock], results)

        report = self._report(manifest, evidence)
        cell = self._cell_report(report, plan.name)

        self.assertEqual(cell.decision, "supports_publication")
        self.assertEqual(cell.evaluation.held_out_count if cell.evaluation else None, 16)
        self.assertEqual(cell.evaluation.range_hit_count if cell.evaluation else None, 13)
        self.assertEqual(cell.guided_candidate_win_count, 13)
        self.assertEqual(cell.guided_candidate_tie_count, 3)
        self.assertLess(cell.range_hit_p_value or 1, 0.025)
        self.assertLess(cell.guided_candidate_p_value or 1, 0.025)
        self.assertEqual(cell.duplicate_review_count, 4)
        self.assertEqual(cell.duplicate_review_agreement, 1.0)
        self.assertEqual(cell.blocking_reasons, ())
        self.assertEqual(report.evidence_state, "incomplete")
        self.assertFalse(report.supports_publication_review)

    def test_safety_regression_rejects_an_otherwise_eligible_candidate(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(manifest, plan.cell_plan_id, ("animation",))
        results = list(self._candidate_results(manifest, plan.cell_plan_id, candidate_lock.candidate_lock_id))
        first = results[0]
        self.assertIsNotNone(first.guided)
        guided = replace(first.guided, visual_accepted=False)
        results[0] = self._result(
            manifest,
            first.case_id,
            plan.cell_plan_id,
            source_index=1,
            candidate_lock_id=candidate_lock.candidate_lock_id,
            baseline=first.baseline,
            guided=guided,
            duplicate_review=False,
        )
        evidence = self._evidence(manifest, [candidate_lock], results)

        report = self._report(manifest, evidence)
        cell = self._cell_report(report, plan.name)

        self.assertEqual(cell.decision, "rejected")
        self.assertIn("safety_regression", cell.blocking_reasons)
        self.assertIn("guided_outcome_not_accepted", cell.blocking_reasons)

    def test_fallback_conformance_preserves_full_search(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "transparent_directional_fallback")
        cases = self._cases(manifest, plan.cell_plan_id)
        results = [
            self._result(
                manifest,
                case.case_id,
                plan.cell_plan_id,
                source_index=index,
                intent_level="transparent",
                traits=("typical",),
                prediction_status="no_recommendation",
                fallback_reason="compression_intent_requires_directional_search",
                baseline=self._arm(selected_crf=31.0, candidate_count=4),
            )
            for index, case in enumerate(cases, start=1)
        ]
        evidence = self._evidence(manifest, [], results)

        report = self._report(manifest, evidence)
        cell = self._cell_report(report, plan.name)

        self.assertEqual(cell.decision, "conformance_passed")
        self.assertEqual(cell.complete_case_count, 3)
        self.assertIsNone(cell.evaluation)
        self.assertEqual(cell.blocking_reasons, ())

    def test_series_reuse_and_local_evidence_block_publication(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(manifest, plan.cell_plan_id, ("animation",))
        results = list(self._candidate_results(manifest, plan.cell_plan_id, candidate_lock.candidate_lock_id))
        first = results[0]
        second = results[1]
        results[1] = self._rebuild_result(
            second,
            series_token=first.series_token,
        )
        results[2] = self._rebuild_result(
            results[2],
            local_evidence_present=True,
        )
        evidence = self._evidence(manifest, [candidate_lock], results)

        report = self._report(manifest, evidence)
        cell = self._cell_report(report, plan.name)

        self.assertEqual(cell.decision, "rejected")
        self.assertIn("held_out_series_reused", cell.blocking_reasons)
        self.assertIn("local_evidence_contamination", cell.blocking_reasons)

    def test_merged_candidate_contract_allows_wide_range_at_type_boundary(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(
            manifest,
            plan.cell_plan_id,
            ("animation",),
            crf_lower=18.0,
            crf_upper=45.0,
        )
        self.assertEqual(candidate_lock.crf_upper - candidate_lock.crf_lower, 27.0)

    def test_derivation_overlap_is_rejected(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(
            manifest,
            plan.cell_plan_id,
            ("animation",),
            derivation_source_tokens=(
                "derivation.source.002",
                "derivation.source.003",
                "derivation.source.004",
                "derivation.source.005",
                "derivation.source.006",
                "source.token.001",
            ),
        )
        results = self._candidate_results(manifest, plan.cell_plan_id, candidate_lock.candidate_lock_id)
        evidence = self._evidence(manifest, [candidate_lock], results)

        report = self._report(manifest, evidence)
        cell = self._cell_report(report, plan.name)

        self.assertEqual(cell.decision, "rejected")
        self.assertIn("derivation_holdout_source_overlap", cell.blocking_reasons)

    def test_duplicate_reviews_are_preregistered_independent_and_safety_bounded(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(manifest, plan.cell_plan_id, ("animation",))
        results = list(self._candidate_results(manifest, plan.cell_plan_id, candidate_lock.candidate_lock_id))
        duplicate_index = next(
            index
            for index, result in enumerate(results)
            if result.duplicate_guided_visual_accepted is not None
        )
        results[duplicate_index] = self._rebuild_result(
            results[duplicate_index],
            duplicate_guided_visual_accepted=False,
        )
        evidence = self._evidence(manifest, [candidate_lock], results)

        report = self._report(manifest, evidence)
        cell = self._cell_report(report, plan.name)

        self.assertEqual(cell.decision, "rejected")
        self.assertIn("duplicate_visual_rejection", cell.blocking_reasons)

    def test_evidence_round_trip_and_report_remove_private_tokens(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(manifest, plan.cell_plan_id, ("animation",))
        results = self._candidate_results(manifest, plan.cell_plan_id, candidate_lock.candidate_lock_id)
        evidence = self._evidence(manifest, [candidate_lock], results)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_bytes(serialize_av1_cold_start_validation_evidence_set(evidence))
            loaded = load_av1_cold_start_validation_evidence_set(path)
        self.assertEqual(loaded, evidence)

        report = self._report(manifest, loaded)
        public_payload = json.dumps(report.to_payload(), sort_keys=True)
        self.assertNotIn("source.token.", public_payload)
        self.assertNotIn("series.token.", public_payload)
        self.assertNotIn("source.group.", public_payload)
        self.assertNotIn("compatibility.v1.test", public_payload)
        self.assertNotIn("sha256:" + "2" * 64, public_payload)

    def test_noncanonical_or_tampered_manifest_is_rejected(self) -> None:
        manifest = self._manifest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest.to_payload(), indent=2), encoding="utf-8")
            with self.assertRaisesRegex(AV1ColdStartValidationError, "not canonical"):
                load_av1_cold_start_validation_manifest(path)

            payload = manifest.to_payload()
            payload["valid_until"] = "2027-02-01T00:00:00Z"
            path.write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n")
            with self.assertRaisesRegex(AV1ColdStartValidationError, "ID does not match"):
                load_av1_cold_start_validation_manifest(path)

    def test_validation_criteria_cannot_weaken_preregistered_gates(self) -> None:
        criteria = self._manifest().criteria
        weakened_values = (
            {"minimum_derivation_evidence_count": 11},
            {"minimum_derivation_source_count": 5},
            {"minimum_holdout_count": 15},
            {"minimum_holdout_source_count": 5},
            {"maximum_derivation_age_days": 181},
            {"maximum_candidate_crf_span": 6.1},
            {"range_hit_null_rate": 0.49},
            {"maximum_one_sided_p_value": 0.026},
            {"maximum_safety_regression_count": 1},
            {"duplicate_review_fraction": 0.19},
            {"minimum_duplicate_review_agreement": 0.79},
            {"confidence_level": "limited"},
            {"confidence_score": 0.69},
        )

        for values in weakened_values:
            with self.subTest(values=values), self.assertRaises(AV1ColdStartValidationError):
                replace(criteria, **values)

    def test_manifest_enforces_criteria_duplicate_review_fraction(self) -> None:
        manifest = self._manifest()
        criteria = replace(manifest.criteria, duplicate_review_fraction=0.3)

        with self.assertRaises(AV1ColdStartValidationError):
            AV1ColdStartValidationManifestV1(
                manifest_id=manifest.manifest_id,
                validation_contract_version=manifest.validation_contract_version,
                predictor_contract_version=manifest.predictor_contract_version,
                governance_issue=manifest.governance_issue,
                state=manifest.state,
                registered_at=manifest.registered_at,
                valid_until=manifest.valid_until,
                execution_policy=manifest.execution_policy,
                privacy_contract=manifest.privacy_contract,
                selection_lock_required=manifest.selection_lock_required,
                criteria=criteria,
                cell_plans=manifest.cell_plans,
                cases=manifest.cases,
                payload_sha256=manifest.payload_sha256,
            )

    def test_pinned_preregistration_rejects_a_canonical_rebuild(self) -> None:
        manifest = self._manifest()
        altered = build_av1_cold_start_validation_manifest(
            registered_at=manifest.registered_at,
            valid_until="2027-02-01T00:00:00Z",
            criteria=manifest.criteria,
            cell_plans=manifest.cell_plans,
        )

        with self.assertRaisesRegex(AV1ColdStartValidationError, "pinned"):
            assert_preregistered_av1_cold_start_validation_manifest(altered)

    def test_execution_and_evidence_chronology_are_enforced(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(
            manifest,
            plan.cell_plan_id,
            ("animation",),
            derivation_newest_recorded_at="2026-07-26T00:00:00Z",
            locked_at="2026-07-26T01:00:00Z",
            reviewed_at="2026-07-26T02:00:00Z",
        )
        results = self._candidate_results(manifest, plan.cell_plan_id, candidate_lock.candidate_lock_id)
        evidence = self._evidence(manifest, [candidate_lock], results)

        with self.assertRaisesRegex(AV1ColdStartValidationError, "candidate lock chronology"):
            self._report(manifest, evidence)

        valid_lock = self._candidate_lock(manifest, plan.cell_plan_id, ("animation",))
        valid_evidence = self._evidence(
            manifest,
            [valid_lock],
            self._candidate_results(manifest, plan.cell_plan_id, valid_lock.candidate_lock_id),
        )
        early_finalization = build_av1_cold_start_validation_evidence_set(
            manifest_id=manifest.manifest_id,
            selection_lock_sha256=valid_evidence.selection_lock_sha256,
            finalized_at="2026-07-27T02:15:00Z",
            candidate_locks=valid_evidence.candidate_locks,
            execution_authorization=valid_evidence.execution_authorization,
            results=valid_evidence.results,
        )
        with self.assertRaisesRegex(AV1ColdStartValidationError, "chronology"):
            self._report(manifest, early_finalization)

    def test_quality_floor_measurement_and_acceptance_are_enforced(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(manifest, plan.cell_plan_id, ("animation",))
        results = list(self._candidate_results(manifest, plan.cell_plan_id, candidate_lock.candidate_lock_id))
        first = results[0]
        self.assertIsNotNone(first.guided)
        results[0] = self._rebuild_result(
            first,
            guided=replace(first.guided, measured_quality_score=1.0, quality_floor_met=True),
        )
        report = self._report(manifest, self._evidence(manifest, [candidate_lock], results))
        cell = self._cell_report(report, plan.name)

        self.assertEqual(cell.decision, "rejected")
        self.assertIn("quality_floor_flag_mismatch", cell.blocking_reasons)

    def test_directional_fallback_requires_the_preregistered_reason(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "transparent_directional_fallback")
        results = [
            self._result(
                manifest,
                case.case_id,
                plan.cell_plan_id,
                source_index=index,
                intent_level="transparent",
                traits=("typical",),
                prediction_status="no_recommendation",
                fallback_reason="no_public_evidence",
                baseline=self._arm(selected_crf=31.0, candidate_count=4),
            )
            for index, case in enumerate(self._cases(manifest, plan.cell_plan_id), start=1)
        ]
        report = self._report(manifest, self._evidence(manifest, [], results))
        cell = self._cell_report(report, plan.name)

        self.assertEqual(cell.decision, "conformance_failed")
        self.assertIn("unexpected_fallback_reason", cell.blocking_reasons)

    def test_report_identity_commits_to_the_private_evidence_digest(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(manifest, plan.cell_plan_id, ("animation",))
        results = list(self._candidate_results(manifest, plan.cell_plan_id, candidate_lock.candidate_lock_id))
        first_evidence = self._evidence(manifest, [candidate_lock], results)
        results[0] = self._rebuild_result(results[0], source_token="source.token.alternate")
        second_evidence = self._evidence(manifest, [candidate_lock], results)

        first_report = self._report(manifest, first_evidence)
        second_report = self._report(manifest, second_evidence)
        self.assertNotEqual(first_evidence.payload_sha256, second_evidence.payload_sha256)
        self.assertEqual(first_report.cell_reports, second_report.cell_reports)
        self.assertNotEqual(first_report.report_id, second_report.report_id)

    def test_conflicting_low_confidence_derivation_is_rejected(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(
            manifest,
            plan.cell_plan_id,
            ("animation",),
            confidence_level="limited",
            confidence_score=0.4,
            derivation_conflict_count=1,
        )
        results = self._candidate_results(manifest, plan.cell_plan_id, candidate_lock.candidate_lock_id)
        report = self._report(manifest, self._evidence(manifest, [candidate_lock], results))
        cell = self._cell_report(report, plan.name)

        self.assertEqual(cell.decision, "rejected")
        self.assertIn("candidate_confidence_insufficient", cell.blocking_reasons)
        self.assertIn("derivation_evidence_conflicting", cell.blocking_reasons)

    def test_merged_candidate_contract_allows_stale_timestamp_at_type_boundary(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(
            manifest,
            plan.cell_plan_id,
            ("animation",),
            derivation_oldest_recorded_at="2025-01-01T00:00:00Z",
        )
        self.assertEqual(
            candidate_lock.derivation_oldest_recorded_at,
            "2025-01-01T00:00:00Z",
        )

    def test_oversized_json_number_uses_the_controlled_error_path(self) -> None:
        payload = self._manifest().to_payload()
        criteria = dict(payload["criteria"])
        criteria["maximum_candidate_crf_span"] = 10 ** 400
        payload["criteria"] = criteria

        with self.assertRaisesRegex(AV1ColdStartValidationError, "too large"):
            av1_cold_start_validation_manifest_from_payload(payload)

    def test_merged_criteria_contract_allows_more_than_twelve_observations(self) -> None:
        criteria = replace(
            self._manifest().criteria,
            minimum_derivation_evidence_count=13,
        )
        self.assertEqual(criteria.minimum_derivation_evidence_count, 13)

    def test_merged_candidate_contract_allows_nonfixed_evidence_count(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(
            manifest,
            plan.cell_plan_id,
            ("animation",),
            derivation_evidence_count=11,
        )
        self.assertEqual(candidate_lock.derivation_evidence_count, 11)

    def test_candidate_lock_payload_keys_match_frozen_v1_schema(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(manifest, plan.cell_plan_id, ("animation",))
        self.assertEqual(
            set(candidate_lock.to_payload()),
            {
                "candidate_lock_id",
                "manifest_id",
                "cell_plan_id",
                "exact_traits",
                "crf_lower",
                "crf_center",
                "crf_upper",
                "compatibility_signature",
                "policy_signature",
                "target_video_bitrate_min_bps",
                "target_video_bitrate_max_bps",
                "minimum_quality_score",
                "confidence_level",
                "confidence_score",
                "derivation_evidence_count",
                "derivation_source_count",
                "derivation_source_tokens",
                "derivation_series_tokens",
                "derivation_source_group_tokens",
                "derivation_oldest_recorded_at",
                "derivation_newest_recorded_at",
                "derivation_conflict_count",
                "derivation_snapshot_sha256",
                "selection_lock_sha256",
                "locked_at",
                "reviewed_at",
                "review_state",
                "payload_sha256",
            },
        )

    def test_result_and_criteria_payload_keys_match_frozen_v1_schema(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(
            manifest,
            plan.cell_plan_id,
            ("animation",),
        )
        result = self._candidate_results(
            manifest,
            plan.cell_plan_id,
            candidate_lock.candidate_lock_id,
        )[0]
        self.assertEqual(
            set(result.to_payload()),
            {
                "result_id",
                "manifest_id",
                "cell_plan_id",
                "case_id",
                "execution_order",
                "completed_at",
                "status",
                "source_token",
                "series_token",
                "source_group_token",
                "content_traits",
                "intent_level",
                "compatibility_signature",
                "policy_signature",
                "review_environment_token",
                "target_video_bitrate_bps",
                "prediction_status",
                "fallback_reason",
                "local_evidence_present",
                "candidate_lock_id",
                "blinded_visual_review",
                "duplicate_guided_visual_accepted",
                "duplicate_review_environment_token",
                "baseline",
                "guided",
                "failure_reason",
                "payload_sha256",
            },
        )
        self.assertEqual(
            set(manifest.criteria.to_payload()),
            {
                "minimum_derivation_evidence_count",
                "minimum_derivation_source_count",
                "minimum_holdout_count",
                "minimum_holdout_source_count",
                "maximum_derivation_age_days",
                "maximum_candidate_crf_span",
                "range_hit_null_rate",
                "maximum_one_sided_p_value",
                "maximum_safety_regression_count",
                "duplicate_review_fraction",
                "minimum_duplicate_review_agreement",
                "confidence_level",
                "confidence_score",
            },
        )

    def test_merged_candidate_contract_allows_zero_quality_floor(self) -> None:
        manifest = self._manifest()
        plan = self._plan(manifest, "animation_balanced_candidate")
        candidate_lock = self._candidate_lock(
            manifest,
            plan.cell_plan_id,
            ("animation",),
            minimum_quality_score=0.0,
        )
        self.assertEqual(candidate_lock.minimum_quality_score, 0.0)

    def test_holdout_authorization_binds_every_publication_candidate_lock(self) -> None:
        manifest = self._manifest()
        locks = self._publication_locks(manifest)
        authorization = self._authorization(manifest, locks)

        self.assertEqual(len(locks), 2)
        self.assertEqual({lock.cell_plan_id for lock in locks}, {
            plan.cell_plan_id
            for plan in manifest.cell_plans
            if plan.mode == "publication_candidate"
        })
        self.assertEqual(
            authorization.candidate_lock_ids,
            tuple(sorted(lock.candidate_lock_id for lock in locks)),
        )
        self.assertEqual(
            set(authorization.to_payload()),
            {
                "authorization_id",
                "manifest_id",
                "selection_lock_sha256",
                "candidate_lock_ids",
                "review_environment_token",
                "authorized_at",
                "review_state",
                "payload_sha256",
            },
        )
        self.assertEqual(
            authorization.authorization_id,
            "av1vauthorization1_ce864ccf6656638bf862a4341cfab23f",
        )
        self.assertEqual(
            authorization.payload_sha256,
            "sha256:d4441d8c0aa9ab015f0e4aab15bcd80b90c8a58af6bea77498d87f141ecec11a",
        )

    def test_holdout_authorization_rejects_incomplete_or_padded_lock_sets(self) -> None:
        manifest = self._manifest()
        animation = self._plan(manifest, "animation_balanced_candidate")
        live_action = self._plan(manifest, "typical_live_action_balanced_candidate")
        fallback = self._plan(manifest, "darkness_risk_fallback")
        animation_lock = self._candidate_lock(manifest, animation.cell_plan_id, ("animation",))
        live_action_lock = self._candidate_lock(manifest, live_action.cell_plan_id, ("typical",))
        lock_sets = {
            "single_lock": (animation_lock,),
            "duplicate_cell": (
                animation_lock,
                self._candidate_lock(
                    manifest,
                    animation.cell_plan_id,
                    ("animation",),
                    crf_lower=28.5,
                ),
            ),
            "wrong_cell": (
                animation_lock,
                self._candidate_lock(manifest, fallback.cell_plan_id, ("darkness",)),
            ),
            "extra_lock": (
                animation_lock,
                live_action_lock,
                self._candidate_lock(manifest, fallback.cell_plan_id, ("darkness",)),
            ),
            "wrong_manifest": (
                animation_lock,
                self._candidate_lock(
                    manifest,
                    live_action.cell_plan_id,
                    ("typical",),
                    manifest_id="av1vmanifest2_" + "0" * 32,
                ),
            ),
            "wrong_traits": (
                animation_lock,
                self._candidate_lock(manifest, live_action.cell_plan_id, ("animation",)),
            ),
            "mixed_selection_locks": (
                animation_lock,
                self._candidate_lock(
                    manifest,
                    live_action.cell_plan_id,
                    ("typical",),
                    selection_lock_sha256="sha256:" + "3" * 64,
                ),
            ),
        }

        for name, locks in lock_sets.items():
            with self.subTest(name=name), self.assertRaises(AV1ColdStartValidationError):
                self._authorization(manifest, locks)

        with self.assertRaises(AV1ColdStartValidationError):
            self._authorization(
                manifest,
                (animation_lock, live_action_lock),
                selection_lock_sha256="sha256:" + "4" * 64,
            )
        with self.assertRaises(AV1ColdStartValidationError):
            replace(animation_lock, review_state="pending_review")

    def _manifest(self) -> AV1ColdStartValidationManifestV1:
        return build_preregistered_av1_cold_start_validation_manifest()

    def _candidate_lock(
            self,
            manifest: AV1ColdStartValidationManifestV1,
            cell_plan_id: str,
            traits: Sequence[str],
            crf_lower: float = 28.0,
            crf_upper: float = 30.0,
            confidence_level: str = "moderate",
            confidence_score: float = 0.7,
            derivation_evidence_count: int = 12,
            minimum_quality_score: float = 93.0,
            derivation_oldest_recorded_at: str = "2026-07-20T00:00:00Z",
            derivation_newest_recorded_at: str = "2026-07-26T00:00:00Z",
            derivation_conflict_count: int = 0,
            locked_at: str = "2026-07-27T01:00:00Z",
            reviewed_at: str = "2026-07-27T02:00:00Z",
            derivation_source_tokens: Sequence[str] = (
                "derivation.source.001",
                "derivation.source.002",
                "derivation.source.003",
                "derivation.source.004",
                "derivation.source.005",
                "derivation.source.006",
            ),
            selection_lock_sha256: str = "sha256:" + "2" * 64,
            manifest_id: str | None = None,
    ) -> AV1ColdStartValidationCandidateLockV1:
        return build_av1_cold_start_validation_candidate_lock(
            manifest_id=manifest_id or manifest.manifest_id,
            cell_plan_id=cell_plan_id,
            exact_traits=traits,
            crf_lower=crf_lower,
            crf_center=29.0,
            crf_upper=crf_upper,
            compatibility_signature="compatibility.v1.test",
            policy_signature="policy.v1.test",
            target_video_bitrate_min_bps=2_000_000,
            target_video_bitrate_max_bps=4_000_000,
            minimum_quality_score=minimum_quality_score,
            confidence_level=confidence_level,
            confidence_score=confidence_score,
            derivation_evidence_count=derivation_evidence_count,
            derivation_source_count=6,
            derivation_source_tokens=derivation_source_tokens,
            derivation_series_tokens=tuple(
                f"derivation.series.{index:03d}" for index in range(1, 13)
            ),
            derivation_source_group_tokens=tuple(
                f"derivation.group.{index:03d}" for index in range(1, 7)
            ),
            derivation_oldest_recorded_at=derivation_oldest_recorded_at,
            derivation_newest_recorded_at=derivation_newest_recorded_at,
            derivation_conflict_count=derivation_conflict_count,
            derivation_snapshot_sha256="sha256:" + "1" * 64,
            selection_lock_sha256=selection_lock_sha256,
            locked_at=locked_at,
            reviewed_at=reviewed_at,
        )

    def _publication_locks(
            self,
            manifest: AV1ColdStartValidationManifestV1,
    ) -> tuple[AV1ColdStartValidationCandidateLockV1, ...]:
        return tuple(
            self._candidate_lock(manifest, plan.cell_plan_id, plan.trait_selector.traits)
            for plan in manifest.cell_plans
            if plan.mode == "publication_candidate"
        )

    def _authorization(
            self,
            manifest: AV1ColdStartValidationManifestV1,
            locks: Sequence[AV1ColdStartValidationCandidateLockV1],
            selection_lock_sha256: str = "sha256:" + "2" * 64,
    ) -> AV1ColdStartValidationExecutionAuthorizationV1:
        return build_av1_cold_start_validation_execution_authorization(
            manifest=manifest,
            selection_lock_sha256=selection_lock_sha256,
            candidate_locks=locks,
            review_environment_token="review.environment.authorization",
            authorized_at="2026-07-27T02:30:00Z",
        )

    def _candidate_results(
            self,
            manifest: AV1ColdStartValidationManifestV1,
            cell_plan_id: str,
            candidate_lock_id: str,
    ) -> tuple[AV1ColdStartValidationResultV1, ...]:
        cases = self._cases(manifest, cell_plan_id)
        return tuple(
            self._result(
                manifest,
                case.case_id,
                cell_plan_id,
                source_index=index,
                candidate_lock_id=candidate_lock_id,
                baseline=self._arm(
                    selected_crf=29.0 if index <= 13 else 31.0,
                    candidate_count=4,
                ),
                guided=self._arm(
                    selected_crf=29.0,
                    candidate_count=2 if index <= 13 else 4,
                ),
                duplicate_review=case.ordinal in {4, 8, 12, 16},
            )
            for index, case in enumerate(cases, start=1)
        )

    def _result(
            self,
            manifest: AV1ColdStartValidationManifestV1,
            case_id: str,
            cell_plan_id: str,
            *,
            source_index: int,
            intent_level: CompressionIntentLevel = "balanced",
            traits: Sequence[str] = ("animation",),
            prediction_status: AV1ColdStartValidationPredictionStatus = "recommended",
            fallback_reason: str | None = None,
            candidate_lock_id: str | None = None,
            baseline: AV1ColdStartValidationArmResultV1 | None = None,
            guided: AV1ColdStartValidationArmResultV1 | None = None,
            duplicate_review: bool = False,
    ) -> AV1ColdStartValidationResultV1:
        case = next(case for case in manifest.cases if case.case_id == case_id)
        return build_av1_cold_start_validation_result(
            manifest_id=manifest.manifest_id,
            cell_plan_id=cell_plan_id,
            case_id=case_id,
            execution_order=case.execution_order,
            completed_at=f"2026-07-27T{source_index + 2:02d}:00:00Z",
            status="complete",
            source_token=f"source.token.{source_index:03d}",
            series_token=f"series.token.{source_index:03d}",
            source_group_token=f"source.group.{((source_index - 1) % 6) + 1:02d}",
            content_traits=traits,
            intent_level=intent_level,
            compatibility_signature="compatibility.v1.test",
            policy_signature="policy.v1.test",
            review_environment_token="review.environment.test",
            target_video_bitrate_bps=3_000_000,
            prediction_status=prediction_status,
            fallback_reason=fallback_reason,
            candidate_lock_id=candidate_lock_id,
            blinded_visual_review=guided is not None,
            duplicate_guided_visual_accepted=(guided.visual_accepted if duplicate_review and guided else None),
            duplicate_review_environment_token=(
                "review.environment.duplicate" if duplicate_review else None
            ),
            baseline=baseline,
            guided=guided,
        )

    def _rebuild_result(
            self,
            result: AV1ColdStartValidationResultV1,
            **changes: Any,
    ) -> AV1ColdStartValidationResultV1:
        values: dict[str, Any] = {
            "manifest_id": result.manifest_id,
            "cell_plan_id": result.cell_plan_id,
            "case_id": result.case_id,
            "execution_order": result.execution_order,
            "completed_at": result.completed_at,
            "status": result.status,
            "source_token": result.source_token,
            "series_token": result.series_token,
            "source_group_token": result.source_group_token,
            "content_traits": result.content_traits,
            "intent_level": result.intent_level,
            "compatibility_signature": result.compatibility_signature,
            "policy_signature": result.policy_signature,
            "review_environment_token": result.review_environment_token,
            "target_video_bitrate_bps": result.target_video_bitrate_bps,
            "prediction_status": result.prediction_status,
            "fallback_reason": result.fallback_reason,
            "local_evidence_present": result.local_evidence_present,
            "candidate_lock_id": result.candidate_lock_id,
            "blinded_visual_review": result.blinded_visual_review,
            "duplicate_guided_visual_accepted": result.duplicate_guided_visual_accepted,
            "duplicate_review_environment_token": result.duplicate_review_environment_token,
            "baseline": result.baseline,
            "guided": result.guided,
            "failure_reason": result.failure_reason,
        }
        values.update(changes)
        return build_av1_cold_start_validation_result(**values)

    def _evidence(
            self,
            manifest: AV1ColdStartValidationManifestV1,
            locks: Sequence[AV1ColdStartValidationCandidateLockV1],
            results: Sequence[AV1ColdStartValidationResultV1],
    ) -> AV1ColdStartValidationEvidenceSetV1:
        resolved_locks = list(locks)
        locked_plan_ids = {candidate_lock.cell_plan_id for candidate_lock in resolved_locks}
        for plan in manifest.cell_plans:
            if plan.mode == "publication_candidate" and plan.cell_plan_id not in locked_plan_ids:
                resolved_locks.append(
                    self._candidate_lock(
                        manifest,
                        plan.cell_plan_id,
                        plan.trait_selector.traits,
                    )
                )
        authorization = build_av1_cold_start_validation_execution_authorization(
            manifest=manifest,
            selection_lock_sha256="sha256:" + "2" * 64,
            candidate_locks=resolved_locks,
            review_environment_token="review.environment.authorization",
            authorized_at="2026-07-27T02:30:00Z",
        )
        return build_av1_cold_start_validation_evidence_set(
            manifest_id=manifest.manifest_id,
            selection_lock_sha256="sha256:" + "2" * 64,
            finalized_at="2026-07-28T00:00:00Z",
            candidate_locks=resolved_locks,
            execution_authorization=authorization,
            results=results,
        )

    @staticmethod
    def _report(
            manifest: AV1ColdStartValidationManifestV1,
            evidence: AV1ColdStartValidationEvidenceSetV1 | None,
    ) -> AV1ColdStartValidationReportV1:
        return build_av1_cold_start_validation_report(
            manifest,
            evidence,
            as_of="2026-07-28T00:00:00Z",
            runtime_state="paused",
        )

    @staticmethod
    def _arm(*, selected_crf: float, candidate_count: int) -> AV1ColdStartValidationArmResultV1:
        return AV1ColdStartValidationArmResultV1(
            selected_crf=selected_crf,
            candidate_count=candidate_count,
            search_seconds=float(candidate_count),
            workflow_seconds=float(candidate_count + 1),
            measured_quality_score=95.0,
            final_size_bytes=100_000_000,
            quality_floor_met=True,
            final_size_met=True,
            visual_accepted=True,
            operator_attention_events=1,
        )

    @staticmethod
    def _plan(
            manifest: AV1ColdStartValidationManifestV1,
            name: str,
    ) -> AV1ColdStartValidationCellPlanV1:
        return next(plan for plan in manifest.cell_plans if plan.name == name)

    @staticmethod
    def _cases(
            manifest: AV1ColdStartValidationManifestV1,
            cell_plan_id: str,
    ) -> list[AV1ColdStartValidationCaseV1]:
        return [case for case in manifest.cases if case.cell_plan_id == cell_plan_id]

    @staticmethod
    def _cell_report(
            report: AV1ColdStartValidationReportV1,
            name: str,
    ) -> AV1ColdStartValidationCellReportV1:
        return next(cell for cell in report.cell_reports if cell.name == name)


if __name__ == "__main__":
    unittest.main()
