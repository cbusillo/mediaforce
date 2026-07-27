import copy
import inspect
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest

from mediaforce.encoding.quality import QualitySearchResult, QualitySearchWarmStart, SampleEncodeResult
from mediaforce.tuning.av1_cold_start import load_packaged_av1_cold_start_bundle
from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1ColdStartValidationCellPlanV1,
    build_av1_cold_start_validation_candidate_lock,
    build_av1_cold_start_validation_execution_authorization,
    load_av1_cold_start_validation_manifest,
)
from mediaforce.tuning.av1_validation_harness import (
    AV1ValidationCandidateContextV1,
    AV1ValidationHarnessDecisionV1,
    AV1ValidationHarnessError,
    AV1ValidationHarnessMachineBindingV1,
    AV1ValidationHarnessWorkV1,
    av1_validation_harness_trace_from_payload,
    build_av1_validation_candidate_context,
    build_av1_validation_harness_machine_binding,
    build_av1_validation_harness_trace,
    load_av1_validation_harness_trace,
    plan_av1_validation_harness_case,
    run_av1_validation_target_size_search,
    serialize_av1_validation_harness_trace,
)
from mediaforce.tuning.size_goals import SizeGoalIntent
from mediaforce.tuning.stream_budget import StreamBudgetLedger, resolve_stream_budget_ledger
from mediaforce.tuning.target_size_search import build_target_size_transform_plan, search_target_size
from mediaforce.web.runtime import calibration_runtime


MANIFEST_PATH = Path("docs/validation/av1-cold-start-preregistration-v1.json")


class AV1ValidationHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        load_packaged_av1_cold_start_bundle.cache_clear()
        self.manifest = load_av1_cold_start_validation_manifest(MANIFEST_PATH)
        self.transform_plan = build_target_size_transform_plan(
            cadence_evidence_id="ev1_cadence",
            cadence_class="progressive",
            cadence_transform="none",
            video_filter=None,
        )
        self.ledger = self._ledger()

    def test_guided_rejected_probe_precedes_unchanged_measured_fallback(self) -> None:
        plan, machine, context = self._candidate_fixture(crf_center=20.0)
        decision = plan_av1_validation_harness_case(
            manifest=self.manifest,
            plan=plan,
            machine_binding=machine,
            mode="guided",
            candidate_context=context,
        )
        baseline_result, baseline_seen = self._run_search(None)
        guided_result, guided_seen = self._run_search(None, decision=decision)

        trace = build_av1_validation_harness_trace(
            decision=decision,
            machine_binding=machine,
            target_size_trace=guided_result.target_size_trace or {},
        )

        self.assertEqual(guided_seen, [20, *baseline_seen])
        self.assertEqual(
            [candidate["crf"] for candidate in (guided_result.target_size_trace or {})["candidates"]],
            [candidate["crf"] for candidate in (baseline_result.target_size_trace or {})["candidates"]],
        )
        self.assertTrue(trace.fallback_invoked)
        self.assertEqual(trace.fallback_reason, "target_band_miss")
        self.assertEqual(trace.ordered_work[0].role, "locked_first_probe")
        self.assertEqual(trace.ordered_work[0].crf, 20.0)
        self.assertTrue(all(work.role == "measured_full_search_probe" for work in trace.ordered_work[1:]))

    def test_guided_accepted_probe_is_exactly_one_search_step(self) -> None:
        plan, machine, context = self._candidate_fixture(crf_center=26.0)
        decision = plan_av1_validation_harness_case(
            manifest=self.manifest,
            plan=plan,
            machine_binding=machine,
            mode="guided",
            candidate_context=context,
        )
        result, seen = self._run_search(None, decision=decision)

        trace = build_av1_validation_harness_trace(
            decision=decision,
            machine_binding=machine,
            target_size_trace=result.target_size_trace or {},
        )

        self.assertEqual(seen, [26])
        self.assertFalse(trace.fallback_invoked)
        self.assertEqual(len(trace.ordered_work), 1)
        self.assertEqual(trace.ordered_work[0].role, "locked_first_probe")

    def test_baseline_never_receives_validation_candidate(self) -> None:
        plan, machine, context = self._candidate_fixture(crf_center=20.0)
        with self.assertRaises(AV1ValidationHarnessError):
            plan_av1_validation_harness_case(
                manifest=self.manifest,
                plan=plan,
                machine_binding=machine,
                mode="baseline",
                candidate_context=context,
            )
        decision = plan_av1_validation_harness_case(
            manifest=self.manifest,
            plan=plan,
            machine_binding=machine,
            mode="baseline",
        )
        result, seen = self._run_search(None, decision=decision)
        trace = build_av1_validation_harness_trace(
            decision=decision,
            machine_binding=machine,
            target_size_trace=result.target_size_trace or {},
        )

        self.assertEqual(seen, [31, 18, 24, 28, 26])
        self.assertEqual(trace.prediction_status, "baseline")
        self.assertFalse(trace.fallback_invoked)
        self.assertTrue(all(work.role == "measured_full_search_probe" for work in trace.ordered_work))

    def test_directional_and_risk_fallbacks_are_exact_and_unguided(self) -> None:
        cases = (
            ("reference_directional_fallback", "compression_intent_requires_directional_search", "reference"),
            ("darkness_risk_fallback", "content_profile_uncovered", "balanced"),
        )
        for name, expected_reason, intent_level in cases:
            with self.subTest(name=name):
                plan = self._plan(name)
                machine = self._machine()
                decision = plan_av1_validation_harness_case(
                    manifest=self.manifest,
                    plan=plan,
                    machine_binding=machine,
                    mode="guided",
                )
                result, _seen = self._run_search(None, intent_level=intent_level, decision=decision)
                trace = build_av1_validation_harness_trace(
                    decision=decision,
                    machine_binding=machine,
                    target_size_trace=result.target_size_trace or {},
                )

                self.assertIsNone(decision.search_hint())
                self.assertEqual(trace.fallback_reason, expected_reason)
                self.assertTrue(trace.fallback_invoked)
                self.assertTrue(all(work.role == "measured_full_search_probe" for work in trace.ordered_work))

    def test_candidate_context_fails_closed_before_search(self) -> None:
        plan, machine, context = self._candidate_fixture(crf_center=20.0)
        with self.assertRaises(AV1ValidationHarnessError):
            plan_av1_validation_harness_case(
                manifest=self.manifest,
                plan=plan,
                machine_binding=machine,
                mode="guided",
                candidate_context=context,
                local_evidence_present=True,
            )
        mismatched_machine = build_av1_validation_harness_machine_binding(
            **{
                **machine.semantic_payload(),
                "policy_signature": "policy_other",
            }
        )
        with self.assertRaises(AV1ValidationHarnessError):
            plan_av1_validation_harness_case(
                manifest=self.manifest,
                plan=plan,
                machine_binding=mismatched_machine,
                mode="guided",
                candidate_context=context,
            )

    def test_missing_or_tampered_machine_trace_fails_closed(self) -> None:
        plan, machine, context = self._candidate_fixture(crf_center=20.0)
        decision = plan_av1_validation_harness_case(
            manifest=self.manifest,
            plan=plan,
            machine_binding=machine,
            mode="guided",
            candidate_context=context,
        )
        result, _seen = self._run_search(None, decision=decision)
        missing = copy.deepcopy(result.target_size_trace or {})
        missing.pop("warm_start")
        with self.assertRaises(AV1ValidationHarnessError):
            build_av1_validation_harness_trace(
                decision=decision,
                machine_binding=machine,
                target_size_trace=missing,
            )
        tampered = copy.deepcopy(result.target_size_trace or {})
        tampered["warm_start"]["candidate_crf"] = 21
        with self.assertRaises(AV1ValidationHarnessError):
            build_av1_validation_harness_trace(
                decision=decision,
                machine_binding=machine,
                target_size_trace=tampered,
            )

    def test_trace_round_trip_is_canonical_and_public_safe(self) -> None:
        plan, machine, context = self._candidate_fixture(crf_center=26.0)
        decision = plan_av1_validation_harness_case(
            manifest=self.manifest,
            plan=plan,
            machine_binding=machine,
            mode="guided",
            candidate_context=context,
        )
        result, _seen = self._run_search(None, decision=decision)
        trace = build_av1_validation_harness_trace(
            decision=decision,
            machine_binding=machine,
            target_size_trace=result.target_size_trace or {},
        )
        serialized = serialize_av1_validation_harness_trace(trace)

        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.json"
            trace_path.write_bytes(serialized)
            loaded = load_av1_validation_harness_trace(trace_path)
            self.assertEqual(loaded, trace)
            trace_path.write_text(json.dumps(trace.to_payload(), indent=2), encoding="utf-8")
            with self.assertRaises(AV1ValidationHarnessError):
                load_av1_validation_harness_trace(trace_path)

        flattened = serialized.decode("utf-8")
        for forbidden in ("/Users/", "rel_path", "source_id", "fingerprint", "review_media"):
            self.assertNotIn(forbidden, flattened)

    def test_production_search_rejects_validation_source(self) -> None:
        plan, machine, context = self._candidate_fixture(crf_center=20.0)
        decision = plan_av1_validation_harness_case(
            manifest=self.manifest,
            plan=plan,
            machine_binding=machine,
            mode="guided",
            candidate_context=context,
        )

        result, seen = self._run_search(decision.search_hint())
        trace = build_av1_validation_harness_trace(
            decision=decision,
            machine_binding=machine,
            target_size_trace=result.target_size_trace or {},
        )

        self.assertNotEqual(seen[0], 20)
        self.assertEqual((result.target_size_trace or {})["warm_start"]["status"], "guard_rejected")
        self.assertFalse((result.target_size_trace or {})["warm_start"]["attempted"])
        self.assertTrue(trace.fallback_invoked)
        self.assertEqual(trace.fallback_reason, "invalid_hint_contract")
        self.assertEqual(trace.ordered_work[0].status, "guard_rejected")

    def test_work_claims_require_coherent_measured_flags(self) -> None:
        with self.assertRaises(AV1ValidationHarnessError):
            AV1ValidationHarnessWorkV1(1, "locked_first_probe", 20.0, "probe_error", True, True)
        with self.assertRaises(AV1ValidationHarnessError):
            AV1ValidationHarnessWorkV1(1, "locked_first_probe", 20.0, "measured", None, None)

    def test_string_typed_machine_claims_are_rejected(self) -> None:
        plan, machine, context = self._candidate_fixture(crf_center=20.0)
        decision = plan_av1_validation_harness_case(
            manifest=self.manifest,
            plan=plan,
            machine_binding=machine,
            mode="guided",
            candidate_context=context,
        )
        result, _seen = self._run_search(None, decision=decision)
        tampered = copy.deepcopy(result.target_size_trace or {})
        tampered["warm_start"]["candidate_crf"] = "20"
        with self.assertRaises(AV1ValidationHarnessError):
            build_av1_validation_harness_trace(
                decision=decision,
                machine_binding=machine,
                target_size_trace=tampered,
            )
        tampered = copy.deepcopy(result.target_size_trace or {})
        tampered["candidates"][0]["quality_floor_met"] = "false"
        with self.assertRaises(AV1ValidationHarnessError):
            build_av1_validation_harness_trace(
                decision=decision,
                machine_binding=machine,
                target_size_trace=tampered,
            )

    def test_public_bundle_and_production_runtime_remain_unmodified(self) -> None:
        before = load_packaged_av1_cold_start_bundle()
        self._candidate_fixture(crf_center=20.0)
        load_packaged_av1_cold_start_bundle.cache_clear()
        after = load_packaged_av1_cold_start_bundle()

        self.assertEqual(before, after)
        self.assertIsNotNone(after.bundle)
        assert after.bundle is not None
        self.assertEqual(after.bundle.state, "validation_pending")
        self.assertEqual(after.bundle.cells, ())
        self.assertNotIn("av1_validation_harness", inspect.getsource(calibration_runtime))

    def test_payload_parser_rejects_operator_claimed_success(self) -> None:
        with self.assertRaises(AV1ValidationHarnessError):
            av1_validation_harness_trace_from_payload({
                "schema": "mediaforce.av1_validation_harness_trace",
                "schema_version": 1,
                "trace_id": "operator-success",
            })

    def _candidate_fixture(
            self,
            *,
            crf_center: float,
    ) -> tuple[
        AV1ColdStartValidationCellPlanV1,
        AV1ValidationHarnessMachineBindingV1,
        AV1ValidationCandidateContextV1,
    ]:
        plan = self._plan("typical_live_action_balanced_candidate")
        selection_lock_sha256 = f"sha256:{'a' * 64}"
        target_bitrate = int(self.ledger.remaining_video_bitrate_bps or 0)
        candidate_lock = build_av1_cold_start_validation_candidate_lock(
            manifest_id=self.manifest.manifest_id,
            cell_plan_id=plan.cell_plan_id,
            exact_traits=("typical",),
            crf_lower=crf_center - 2,
            crf_center=crf_center,
            crf_upper=crf_center + 2,
            compatibility_signature="compatibility_test",
            policy_signature="policy_test",
            target_video_bitrate_min_bps=target_bitrate - 1_000,
            target_video_bitrate_max_bps=target_bitrate + 1_000,
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
            derivation_snapshot_sha256=f"sha256:{'b' * 64}",
            selection_lock_sha256=selection_lock_sha256,
            locked_at="2026-07-20T00:00:00Z",
            reviewed_at="2026-07-21T00:00:00Z",
        )
        authorization = build_av1_cold_start_validation_execution_authorization(
            manifest_id=self.manifest.manifest_id,
            selection_lock_sha256=selection_lock_sha256,
            candidate_lock_ids=(candidate_lock.candidate_lock_id,),
            review_environment_token="review_environment",
            authorized_at="2026-07-22T00:00:00Z",
        )
        machine = self._machine()
        context = build_av1_validation_candidate_context(
            manifest=self.manifest,
            plan=plan,
            candidate_lock=candidate_lock,
            authorization=authorization,
            machine_binding=machine,
            configured_min_crf=18,
            configured_max_crf=38,
        )
        return plan, machine, context

    def _machine(self) -> AV1ValidationHarnessMachineBindingV1:
        return build_av1_validation_harness_machine_binding(
            compatibility_signature="compatibility_test",
            policy_signature="policy_test",
            search_signature_id="qms1_harness",
            target_video_bitrate_bps=int(self.ledger.remaining_video_bitrate_bps or 0),
            target_video_bytes=self.ledger.remaining_video_bytes,
            quality_metric="VMAF",
            quality_target=85.0,
            minimum_quality_score=80.0,
            transform_plan_id=str(self.transform_plan["transform_plan_id"]),
            encoder="libsvtav1",
            encoder_version="SVT-AV1 Encoder Lib v3.0.2",
            encoder_runtime_signature_id="erti1_harness",
            quality_tool="ab-av1",
            quality_tool_version="ab-av1 0.9.1",
            metric_runtime_signature_id="qmri1_harness",
        )

    def _run_search(
            self,
            warm_start: QualitySearchWarmStart | None,
            *,
            intent_level: str = "balanced",
            decision: AV1ValidationHarnessDecisionV1 | None = None,
    ) -> tuple[QualitySearchResult, list[int]]:
        seen: list[int] = []

        def run_sample(_source_path: Path, *, crf: float, **_: Any) -> SampleEncodeResult:
            crf_int = int(crf)
            seen.append(crf_int)
            predicted_video_bytes = 1_000_000_000 - 28_000_000 * crf_int
            return SampleEncodeResult(
                "VMAF",
                88.0 - (crf_int - 18) * 0.2,
                predicted_video_bytes / 10_000_000,
                30.0,
                predicted_video_bytes,
                f"crf {crf_int}",
                crf_int * 100_000,
            )

        policy = self._policy()
        if intent_level != "balanced":
            policy.update({
                "compression_intent_schema_version": 1,
                "compression_intent": intent_level,
                "compression_intent_source": "operator",
                "compression_intent_confirmed": True,
            })
        args = (Path("/tmp/source.mkv"), policy)
        kwargs = dict(
            source_codec="h264",
            metric_name="vmaf",
            metric_target=85.0,
            min_metric_score=80.0,
            preset=4,
            pixel_format="yuv420p10le",
            sample_every="8m",
            sample_duration="20s",
            min_crf=18,
            max_crf=38,
            svt_params=[],
            video_filter=None,
            stream_budget_ledger=self.ledger,
            transform_plan=self.transform_plan,
            process_controller=None,
            host=None,
            quality_temp_dir=None,
            run_sample_encode=run_sample,
        )
        if decision is None:
            result = search_target_size(
                *args,
                warm_start=warm_start,
                expected_search_signature_id="qms1_harness" if warm_start else None,
                **kwargs,
            )
        else:
            result = run_av1_validation_target_size_search(
                *args,
                decision=decision,
                **kwargs,
            )
        return result, seen

    def _ledger(self) -> StreamBudgetLedger:
        item = {
            "library_item_id": 7,
            "rel_path": "tv/show/episode.mkv",
            "source_fingerprint": "fingerprint-7",
            "source_size_bytes": 1_000_000_000,
            "video_bitrate": 8_000_000,
            "duration_seconds": 3600.0,
            "output_container": "mkv",
            "resolved_policy": self._policy(),
            "audio_summary": [],
            "subtitle_summary": [],
            "attachment_summary": [],
        }
        size_goal = SizeGoalIntent(
            mode="absolute",
            value_bytes=300_000_000,
            reference_runtime_seconds=None,
            sample_projection_tolerance_percent=10.0,
            final_output_tolerance_percent=5.0,
            source="test",
        ).resolve(3600.0)
        return resolve_stream_budget_ledger(item, resolved_size_goal=size_goal, prefer_persisted=False)

    @staticmethod
    def _policy() -> dict[str, object]:
        return {
            "video": {
                "encoder": "libsvtav1",
                "pixel_format": "yuv420p10le",
                "preset": 4,
                "quality_metric": "vmaf",
                "target_vmaf": 85.0,
                "min_target_vmaf": 80.0,
                "sample_every": "8m",
                "sample_duration": "20s",
                "min_crf": 18,
                "max_crf": 38,
                "max_encoded_percent": 80,
                "default_grain": 0,
                "grain_denoise": 0,
            },
            "audio": {},
            "subtitle": {},
        }

    def _plan(self, name: str) -> AV1ColdStartValidationCellPlanV1:
        return next(plan for plan in self.manifest.cell_plans if plan.name == name)


if __name__ == "__main__":
    unittest.main()
