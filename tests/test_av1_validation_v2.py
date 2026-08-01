import copy
import json
from pathlib import Path
import tempfile
from typing import cast
import unittest

from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1ColdStartValidationCandidateLockV1,
    AV1ColdStartValidationCellPlanV1,
    AV1ColdStartValidationError,
    AV1ColdStartValidationManifestV1,
    build_av1_cold_start_validation_candidate_lock,
    build_av1_cold_start_validation_execution_authorization,
    load_av1_cold_start_validation_manifest,
)
from mediaforce.tuning.av1_validation_harness import (
    AV1ValidationHarnessDecisionV1,
    AV1ValidationHarnessError,
    build_av1_validation_candidate_context,
    build_av1_validation_harness_machine_binding,
    build_av1_validation_harness_trace,
    plan_av1_validation_harness_case,
)
from mediaforce.tuning.av1_validation_v2 import (
    AV1_VALIDATION_V1_MANIFEST_ID,
    AV1_VALIDATION_V1_MANIFEST_PAYLOAD_SHA256,
    AV1ValidationV2EligibilityCell,
    AV1ValidationV2Error,
    assert_preregistered_av1_validation_manifest_v2,
    av1_validation_manifest_v2_from_payload,
    av1_validation_v2_derivation_authorization_from_payload,
    build_av1_validation_v2_derivation_authorization,
    build_preregistered_av1_validation_manifest_v2,
    load_av1_validation_manifest_v2,
    serialize_av1_validation_manifest_v2,
)


V1_MANIFEST_PATH = Path("docs/validation/av1-cold-start-preregistration-v1.json")
V2_MANIFEST_PATH = Path("docs/validation/av1-cold-start-preregistration-v2.json")
V2_SELECTION_LOCK_SHA256 = f"sha256:{'c' * 64}"


class AV1ValidationV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_av1_validation_manifest_v2(V2_MANIFEST_PATH)

    def test_checked_in_manifest_is_canonical_and_v1_is_unchanged(self) -> None:
        expected = build_preregistered_av1_validation_manifest_v2()
        self.assertEqual(self.manifest, expected)
        self.assertEqual(V2_MANIFEST_PATH.read_bytes(), serialize_av1_validation_manifest_v2(expected))
        self.assertEqual(
            self.manifest.payload_sha256,
            "sha256:5a24bbfdfe699aa2c6f037b9473ce06607aee20af743f57266f38bf7eb08d268",
        )
        assert_preregistered_av1_validation_manifest_v2(self.manifest)

        v1 = load_av1_cold_start_validation_manifest(V1_MANIFEST_PATH)
        self.assertEqual(v1.manifest_id, AV1_VALIDATION_V1_MANIFEST_ID)
        self.assertEqual(v1.payload_sha256, AV1_VALIDATION_V1_MANIFEST_PAYLOAD_SHA256)

    def test_manifest_freezes_reachable_cells_and_derivation_only_authority(self) -> None:
        candidates = {
            plan.name: plan
            for plan in self.manifest.cell_plans
            if plan.mode == "publication_candidate"
        }
        fallbacks = {
            plan.name: plan
            for plan in self.manifest.cell_plans
            if plan.mode == "fallback_conformance"
        }
        self.assertEqual(set(candidates), {"darkness_balanced_candidate", "motion_balanced_candidate"})
        self.assertEqual(
            set(fallbacks),
            {
                "grain_noise_risk_fallback",
                "mixed_risk_fallback",
                "perceptual_floor_directional_fallback",
                "reference_directional_fallback",
                "texture_detail_risk_fallback",
                "transparent_directional_fallback",
            },
        )
        self.assertEqual(len(self.manifest.cases), 50)
        self.assertEqual(len(self.manifest.excluded_cells), 4)
        self.assertEqual(
            {cell.name: cell.reason for cell in self.manifest.excluded_cells},
            {
                "animation_balanced_candidate": "insufficient_inventory",
                "low_motion_dialogue_risk_fallback": "trait_projection_unavailable",
                "typical_live_action_balanced_candidate": "insufficient_inventory",
                "unknown_risk_fallback": "request_contract_unavailable",
            },
        )
        payload = self.manifest.to_payload()
        self.assertFalse(payload["runtime_execution_authorized"])
        self.assertFalse(payload["holdout_execution_authorized"])
        self.assertFalse(payload["authority_policy"]["guided_probe_allowed_during_derivation"])
        self.assertFalse(payload["authority_policy"]["holdout_case_execution_allowed_during_derivation"])
        self.assertFalse(payload["execution_semantics"]["unfavorable_or_incomplete_cases_droppable"])

        cases_by_plan: dict[str, list[str]] = {}
        for case in self.manifest.cases:
            cases_by_plan.setdefault(case.cell_plan_id, []).append(case.execution_order)
        for plan in candidates.values():
            self.assertEqual(cases_by_plan[plan.cell_plan_id].count("baseline_then_guided"), 8)
            self.assertEqual(cases_by_plan[plan.cell_plan_id].count("guided_then_baseline"), 8)
            self.assertEqual(plan.duplicate_review_ordinals, (4, 8, 12, 16))
        for plan in fallbacks.values():
            self.assertEqual(cases_by_plan[plan.cell_plan_id], ["baseline_only"] * 3)

    def test_manifest_rejects_any_runtime_authority(self) -> None:
        payload = copy.deepcopy(self.manifest.to_payload())
        payload["runtime_execution_authorized"] = True
        with self.assertRaises(AV1ValidationV2Error):
            av1_validation_manifest_v2_from_payload(payload)

        payload = copy.deepcopy(self.manifest.to_payload())
        payload["execution_semantics"]["unfavorable_or_incomplete_cases_droppable"] = True
        with self.assertRaises(AV1ValidationV2Error):
            av1_validation_manifest_v2_from_payload(payload)

    def test_derivation_authorization_cannot_address_holdouts_or_guided_probes(self) -> None:
        authorization = build_av1_validation_v2_derivation_authorization(
            manifest=self.manifest,
            selection_lock_sha256=f"sha256:{'a' * 64}",
            derivation_partition_sha256=f"sha256:{'b' * 64}",
            authorized_at="2026-08-01T00:00:00Z",
            valid_until="2026-08-02T00:00:00Z",
        )
        payload = authorization.to_payload()
        self.assertTrue(authorization.authorization_id.startswith("av1vderivation1_"))
        self.assertFalse(authorization.authorization_id.startswith("av1vauthorization1_"))
        self.assertTrue(payload["runtime_execution_authorized"])
        self.assertEqual(payload["search_mode"], "unchanged_measured_full_search")
        self.assertFalse(payload["validation_harness_allowed"])
        self.assertFalse(payload["validation_candidate_allowed"])
        self.assertFalse(payload["guided_probe_allowed"])
        self.assertFalse(payload["holdout_case_execution_allowed"])
        self.assertFalse(payload["public_bundle_activation_allowed"])
        self.assertEqual(
            set(payload),
            {
                "authorization_id",
                "schema",
                "schema_version",
                "contract_version",
                "authority",
                "runtime_execution_authorized",
                "execution_scope",
                "search_mode",
                "validation_harness_allowed",
                "validation_candidate_allowed",
                "guided_probe_allowed",
                "holdout_case_execution_allowed",
                "public_bundle_activation_allowed",
                "manifest_id",
                "manifest_payload_sha256",
                "selection_lock_sha256",
                "derivation_partition_sha256",
                "authorized_at",
                "valid_until",
                "review_state",
                "payload_sha256",
            },
        )

        payload["guided_probe_allowed"] = True
        with self.assertRaises(AV1ValidationV2Error):
            av1_validation_v2_derivation_authorization_from_payload(payload)

    def test_eligibility_rejects_unregistered_private_text(self) -> None:
        with self.assertRaises(AV1ValidationV2Error):
            AV1ValidationV2EligibilityCell(
                name="unknown_risk_fallback",
                state="excluded",
                match="contains_all",
                traits=("unknown",),
                required_case_count=3,
                matching_item_count=0,
                distinct_parent_scope_count=0,
                producer_blockers=("private_title_or_path",),
                exclusion_reason="request_contract_unavailable",
            )

    def test_v2_ids_are_accepted_by_the_isolated_harness(self) -> None:
        plan = next(
            plan for plan in self.manifest.cell_plans if plan.name == "motion_balanced_candidate"
        )
        candidate_locks = self._candidate_locks()
        candidate_lock = next(
            lock for lock in candidate_locks if lock.cell_plan_id == plan.cell_plan_id
        )
        execution_authorization = build_av1_cold_start_validation_execution_authorization(
            manifest=cast(AV1ColdStartValidationManifestV1, self.manifest),
            selection_lock_sha256=V2_SELECTION_LOCK_SHA256,
            candidate_locks=candidate_locks,
            review_environment_token="review_environment_v2",
            authorized_at="2026-07-22T00:00:00Z",
        )
        self.assertEqual(
            execution_authorization.candidate_lock_ids,
            tuple(sorted(lock.candidate_lock_id for lock in candidate_locks)),
        )
        machine = build_av1_validation_harness_machine_binding(
            compatibility_signature="compatibility_v2",
            policy_signature="policy_v2",
            search_signature_id="qms1_v2",
            target_video_bitrate_bps=1_000_000,
            target_video_bytes=10_000_000,
            quality_metric="VMAF",
            quality_target=85.0,
            minimum_quality_score=80.0,
            transform_plan_id="transform_v2",
            encoder="libsvtav1",
            encoder_version="SVT-AV1 Encoder Lib v3.0.2",
            encoder_runtime_signature_id="erti1_v2",
            quality_tool="ab-av1",
            quality_tool_version="ab-av1 0.9.1",
            metric_runtime_signature_id="qmri1_v2",
        )
        context = build_av1_validation_candidate_context(
            manifest=cast(AV1ColdStartValidationManifestV1, self.manifest),
            plan=cast(AV1ColdStartValidationCellPlanV1, plan),
            candidate_locks=candidate_locks,
            authorization=execution_authorization,
            machine_binding=machine,
            configured_min_crf=18,
            configured_max_crf=38,
        )
        self.assertEqual(context.manifest_id, self.manifest.manifest_id)
        self.assertEqual(context.candidate_lock_id, candidate_lock.candidate_lock_id)

        with self.assertRaises(AV1ValidationHarnessError):
            build_av1_validation_candidate_context(
                manifest=cast(AV1ColdStartValidationManifestV1, self.manifest),
                plan=cast(AV1ColdStartValidationCellPlanV1, plan),
                candidate_locks=(candidate_lock,),
                authorization=execution_authorization,
                machine_binding=machine,
                configured_min_crf=18,
                configured_max_crf=38,
            )

        baseline = plan_av1_validation_harness_case(
            manifest=cast(AV1ColdStartValidationManifestV1, self.manifest),
            plan=cast(AV1ColdStartValidationCellPlanV1, plan),
            machine_binding=machine,
            mode="baseline",
        )
        trace = build_av1_validation_harness_trace(
            decision=baseline,
            machine_binding=machine,
            target_size_trace={
                "schema_version": 1,
                "status": "selected",
                "selection_reason": "candidate_inside_sample_projection_band",
                "quality_floor": {"metric": "VMAF", "target": 85.0, "minimum": 80.0},
                "target": {"target_video_bytes": 10_000_000},
                "transform_plan": {"transform_plan_id": "transform_v2"},
                "candidates": [{
                    "attempt": 1,
                    "role": "measured_full_search_probe",
                    "crf": 22.0,
                    "quality_floor_met": True,
                    "within_sample_band": True,
                }],
            },
        )
        self.assertEqual(trace.manifest_id, self.manifest.manifest_id)
        self.assertEqual(trace.cell_plan_id, plan.cell_plan_id)

    def test_harness_rejects_unregistered_fallback_text(self) -> None:
        with self.assertRaises(AV1ValidationHarnessError):
            AV1ValidationHarnessDecisionV1(
                manifest_id=self.manifest.manifest_id,
                cell_plan_id=self.manifest.cell_plans[0].cell_plan_id,
                mode="guided",
                prediction_status="no_recommendation",
                intent_level="balanced",
                optimization_objective=(
                    "minimize_target_distance_subject_to_measured_quality_floor"
                ),
                content_traits=("motion",),
                machine_binding_id="av1vhm1_0123456789abcdef0123456789abcdef",
                context=None,
                fallback_reason="private_title_or_path",
                local_evidence_present=False,
            )

    def test_holdout_authorization_requires_both_v2_candidate_locks(self) -> None:
        candidate_locks = self._candidate_locks()
        self.assertEqual(len(candidate_locks), 2)

        for locks in (candidate_locks[:1], candidate_locks[1:], (candidate_locks[0], candidate_locks[0])):
            with self.subTest(count=len(locks)), self.assertRaises(AV1ColdStartValidationError):
                build_av1_cold_start_validation_execution_authorization(
                    manifest=cast(AV1ColdStartValidationManifestV1, self.manifest),
                    selection_lock_sha256=V2_SELECTION_LOCK_SHA256,
                    candidate_locks=locks,
                    review_environment_token="review_environment_v2",
                    authorized_at="2026-07-22T00:00:00Z",
                )

    def _candidate_locks(self) -> tuple[AV1ColdStartValidationCandidateLockV1, ...]:
        return tuple(
            build_av1_cold_start_validation_candidate_lock(
                manifest_id=self.manifest.manifest_id,
                cell_plan_id=plan.cell_plan_id,
                exact_traits=plan.trait_selector.traits,
                crf_lower=20.0,
                crf_center=22.0,
                crf_upper=24.0,
                compatibility_signature="compatibility_v2",
                policy_signature="policy_v2",
                target_video_bitrate_min_bps=999_000,
                target_video_bitrate_max_bps=1_001_000,
                minimum_quality_score=80.0,
                confidence_level="moderate",
                confidence_score=0.8,
                derivation_evidence_count=12,
                derivation_source_count=6,
                derivation_source_tokens=tuple(f"source_{index}" for index in range(6)),
                derivation_series_tokens=tuple(f"series_{index}" for index in range(12)),
                derivation_source_group_tokens=tuple(f"group_{index:03d}" for index in range(6)),
                derivation_oldest_recorded_at="2026-07-01T00:00:00Z",
                derivation_newest_recorded_at="2026-07-10T00:00:00Z",
                derivation_conflict_count=0,
                derivation_snapshot_sha256=f"sha256:{'d' * 64}",
                selection_lock_sha256=V2_SELECTION_LOCK_SHA256,
                locked_at="2026-07-20T00:00:00Z",
                reviewed_at="2026-07-21T00:00:00Z",
            )
            for plan in self.manifest.cell_plans
            if plan.mode == "publication_candidate"
        )

    def test_loader_rejects_noncanonical_bytes(self) -> None:
        payload = json.loads(V2_MANIFEST_PATH.read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            with self.assertRaises(AV1ValidationV2Error):
                load_av1_validation_manifest_v2(path)


if __name__ == "__main__":
    unittest.main()
