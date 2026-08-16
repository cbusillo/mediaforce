import unittest
from types import SimpleNamespace

from mediaforce.core.evidence import stable_policy_hash
from mediaforce.tuning.quality_risk import (
    build_quality_risk_contract,
    compile_allow_listed_transform_fragment,
    quality_risk_public_view,
    with_quality_risk_intent,
)
from mediaforce.tuning.target_size_search import build_target_size_transform_plan
from mediaforce.web.runtime.folder_ai_tuning import _persist_quality_risk_feedback


class QualityRiskContractTests(unittest.TestCase):
    def test_policy_compiler_rejects_unknown_sections_keys_and_invalid_values(self) -> None:
        base_policy = {
            "video": {"quality_metric": "vmaf", "target_vmaf": 90.0},
            "audio": {"keep_languages": ["eng"]},
            "subtitle": {"keep_languages": ["eng"]},
        }

        compiled, applied, rejected = compile_allow_listed_transform_fragment(
            base_policy,
            {
                "video": {"quality_metric": "not-a-metric", "target_vmaf": 88.0},
                "audio": {"keep_languages": ["eng", "jpn"], "replace_layout": "stereo"},
                "container": {"format": "mp4"},
            },
        )

        self.assertEqual(compiled["video"]["target_vmaf"], 88.0)
        self.assertEqual(compiled["audio"]["keep_languages"], ["eng", "jpn"])
        self.assertEqual(applied["video"], {"target_vmaf": 88.0, "min_target_vmaf": 88.0})
        self.assertEqual(
            rejected,
            ["audio.replace_layout", "container", "video.quality_metric"],
        )

    def test_policy_compiler_rejects_out_of_range_and_wrong_typed_values(self) -> None:
        base_policy = {
            "video": {"target_vmaf": 90.0, "preset": 8},
            "audio": {"stereo_opus_bitrate": "128k"},
            "subtitle": {"keep_languages": ["eng"]},
        }

        compiled, applied, rejected = compile_allow_listed_transform_fragment(
            base_policy,
            {
                "video": {"target_vmaf": -999, "preset": 99},
                "audio": {"stereo_opus_bitrate": "-999k"},
                "subtitle": {"keep_languages": [123]},
            },
        )

        self.assertEqual(compiled, base_policy)
        self.assertEqual(applied, {})
        self.assertEqual(
            rejected,
            [
                "audio.stereo_opus_bitrate",
                "subtitle.keep_languages",
                "video.preset",
                "video.target_vmaf",
            ],
        )

    def test_policy_compiler_preserves_larger_absolute_size_goal(self) -> None:
        base_policy = {
            "video": {
                "target_size_mb": 300.0,
                "target_size_bytes": 300_000_000,
                "target_runtime_minutes": 45.0,
            }
        }
        preview_policy = {
            "video": {
                "target_size_mb": 717.0,
                "target_size_bytes": 717_000_000,
                "target_runtime_minutes": 107.581,
            }
        }

        compiled, applied, rejected = compile_allow_listed_transform_fragment(
            base_policy,
            preview_policy,
        )

        self.assertEqual(compiled, preview_policy)
        self.assertEqual(applied, preview_policy)
        self.assertEqual(rejected, [])

    def test_current_rejection_requires_exact_evidence_policy_source_and_job_binding(self) -> None:
        policy = {"video": {"target_vmaf": 90.0}}
        contract = self._contract(
            policy=policy,
            records=[
                self._record(
                    verdict="approved",
                    policy_hash=stable_policy_hash(policy),
                    job_id="job-123",
                    evidence_ids=["ev1_cadence", "ev1_fingerprint"],
                    created_at="2026-03-28T00:00:00+00:00",
                ),
                self._record(
                    verdict="rejected",
                    policy_hash=stable_policy_hash(policy),
                    job_id="job-123",
                    evidence_ids=["ev1_cadence", "ev1_fingerprint"],
                    created_at="2026-03-29T00:00:00+00:00",
                ),
            ],
        )

        public = quality_risk_public_view(contract)
        assert public is not None
        self.assertEqual(public["verdict"], "blocked")
        self.assertEqual(public["cadence_gate"], "progressive")
        self.assertEqual(public["cadence_transform"], "none")
        self.assertEqual(public["operator_decision"]["status"], "rejected")
        self.assertTrue(public["operator_decision"]["effective_record"]["authoritative"])

    def test_newer_approval_supersedes_older_rejection_for_same_binding(self) -> None:
        policy = {"video": {"target_vmaf": 90.0}}
        contract = self._contract(
            policy=policy,
            records=[
                self._record(
                    verdict="rejected",
                    policy_hash=stable_policy_hash(policy),
                    job_id="job-123",
                    evidence_ids=["ev1_cadence", "ev1_fingerprint"],
                    created_at="2026-03-28T00:00:00+00:00",
                ),
                self._record(
                    verdict="approved",
                    policy_hash=stable_policy_hash(policy),
                    job_id="job-123",
                    evidence_ids=["ev1_cadence", "ev1_fingerprint"],
                    created_at="2026-03-29T00:00:00+00:00",
                ),
            ],
        )

        self.assertEqual(contract["operator_decision"]["status"], "approved")
        self.assertEqual(contract["verdict"], "safe_to_sample")

    def test_missing_current_job_and_stale_evidence_never_gain_authority(self) -> None:
        policy = {"video": {"target_vmaf": 90.0}}
        contract = self._contract(
            policy=policy,
            job_id=None,
            records=[
                self._record(
                    verdict="approved",
                    policy_hash=stable_policy_hash(policy),
                    job_id="job-old",
                    evidence_ids=["ev1_cadence"],
                    created_at="2026-03-29T00:00:00+00:00",
                )
            ],
        )

        self.assertEqual(contract["operator_decision"]["status"], "pending")
        self.assertFalse(contract["operator_decision"]["records"][0]["authoritative"])

    def test_model_interpretation_cannot_replace_operator_approval(self) -> None:
        contract = self._contract(
            policy={"video": {"target_vmaf": 90.0}},
            records=[],
            interpretation={
                "summary": "The sample appears safe.",
                "verdict": "safe_to_sample",
                "confidence": "high",
            },
        )

        self.assertEqual(contract["verdict"], "needs_operator_review")

    def test_target_size_quality_conflict_is_typed_without_exposing_raw_candidates(self) -> None:
        trace = self._target_size_trace(status="quality_conflict")
        contract = self._contract(
            policy={"video": {"target_vmaf": 90.0}},
            records=[],
            sample_result={"predicted_encode_percent": 12.0, "target_size_trace": trace},
        )

        public = quality_risk_public_view(contract)
        assert public is not None
        self.assertEqual(public["verdict"], "request_comparison")
        self.assertEqual(public["target_size_search"]["status"], "quality_conflict")
        self.assertEqual(public["target_size_search"]["selected_crf"], 31.0)
        self.assertEqual(public["target_size_search"]["configured_max_crf"], 38)
        self.assertEqual(public["target_size_search"]["search_max_crf"], 63)
        self.assertTrue(public["target_size_search"]["range_expanded"])
        self.assertTrue(public["target_size_search"]["measured_beyond_configured"])
        self.assertNotIn("candidates", public["target_size_search"])
        self.assertIn(
            "softness_detail_loss",
            [risk["tag"] for risk in public["typed_risks"]],
        )

    def test_source_cap_consumed_by_preserved_streams_is_a_deterministic_blocker(self) -> None:
        sample = self._sample()
        sample["stream_budget_ledger"] = {
            "feasibility": {"status": "feasible"},
            "source_relative_cap": {"status": "arithmetically_infeasible"},
        }

        contract = build_quality_risk_contract(
            prefix="tv/House/Season 2",
            sample_item=sample,
            current_policy={"video": {"target_vmaf": 90.0}},
            preview_policy={"video": {"target_vmaf": 90.0}},
            calibration={"job_id": "job-current", "sample_result": {}},
        )

        self.assertTrue(contract["deterministic_gates"]["blocked"])
        self.assertIn(
            "The preserved streams consume the configured source-size cap.",
            contract["deterministic_gates"]["blocking_reasons"],
        )

    def test_old_failed_target_trace_does_not_block_newer_successful_calibration(self) -> None:
        sample = self._sample()
        sample["stream_budget_ledger"] = {
            "ledger_id": "ledger-current",
            "stream_plan": {"plan_id": "plan-current"},
            "feasibility": {"status": "feasible"},
        }
        failed_trace = self._target_size_trace(status="infeasible")
        failed_trace["ledger"] = {
            "source_id": "src-house-s2",
            "ledger_id": "ledger-old",
            "stream_plan_id": "plan-old",
        }

        contract = build_quality_risk_contract(
            prefix="tv/House/Season 2",
            sample_item=sample,
            current_policy={"video": {"target_vmaf": 90.0}},
            preview_policy={"video": {"target_vmaf": 90.0}},
            calibration={"job_id": "job-current", "sample_result": {}},
            latest_failed_sample_job={
                "job_id": "job-old",
                "result": {"target_size_trace": failed_trace},
            },
        )

        self.assertEqual(contract["facts"]["target_size_trace"], {})
        self.assertNotIn("Old failed search.", contract["deterministic_gates"]["blocking_reasons"])

    def test_old_failed_target_trace_does_not_block_fresh_preview_with_new_ledger(self) -> None:
        sample = self._sample()
        sample["stream_budget_ledger"] = {
            "ledger_id": "ledger-current",
            "stream_plan": {"plan_id": "plan-current"},
            "feasibility": {"status": "feasible"},
        }
        failed_trace = self._target_size_trace(status="selected")
        failed_trace["ledger"] = {
            "source_id": "src-house-s2",
            "ledger_id": "ledger-old",
            "stream_plan_id": "plan-old",
        }

        contract = build_quality_risk_contract(
            prefix="tv/House/Season 2",
            sample_item=sample,
            current_policy={"video": {"target_vmaf": 90.0}},
            preview_policy={"video": {"target_vmaf": 90.0, "target_size_mb": 82.0}},
            calibration=None,
            latest_failed_sample_job={
                "job_id": "job-old",
                "result": {"target_size_trace": failed_trace},
            },
        )

        self.assertEqual(contract["facts"]["target_size_trace"], {})
        reasons = contract["deterministic_gates"]["blocking_reasons"]
        self.assertNotIn("The target-size search trace belongs to a different stream-budget ledger.", reasons)
        self.assertNotIn("The target-size search trace belongs to a different production stream plan.", reasons)

    def test_failed_target_trace_remains_current_when_ledger_matches(self) -> None:
        sample = self._sample()
        sample["stream_budget_ledger"] = {
            "ledger_id": "ledger-current",
            "stream_plan": {"plan_id": "plan-current"},
            "feasibility": {"status": "feasible"},
        }
        failed_trace = self._target_size_trace(status="quality_conflict")
        failed_trace["ledger"] = {
            "source_id": "src-house-s2",
            "ledger_id": "ledger-current",
            "stream_plan_id": "plan-current",
        }

        contract = build_quality_risk_contract(
            prefix="tv/House/Season 2",
            sample_item=sample,
            current_policy={"video": {"target_vmaf": 90.0}},
            preview_policy={"video": {"target_vmaf": 90.0}},
            calibration=None,
            latest_failed_sample_job={
                "job_id": "job-current",
                "result": {"target_size_trace": failed_trace},
            },
        )

        self.assertEqual(contract["facts"]["target_size_trace"], failed_trace)

    def test_current_target_trace_must_match_ledger_stream_plan_and_cadence(self) -> None:
        sample = self._sample()
        sample["stream_budget_ledger"] = {
            "ledger_id": "ledger-current",
            "stream_plan": {"plan_id": "plan-current"},
            "feasibility": {"status": "feasible"},
        }
        trace = self._target_size_trace(status="selected")
        trace["ledger"] = {
            "source_id": "src-house-s2",
            "ledger_id": "ledger-old",
            "stream_plan_id": "plan-old",
        }

        contract = build_quality_risk_contract(
            prefix="tv/House/Season 2",
            sample_item=sample,
            current_policy={"video": {"target_vmaf": 90.0}},
            preview_policy={"video": {"target_vmaf": 90.0}},
            calibration={
                "job_id": "job-current",
                "sample_result": {"target_size_trace": trace},
            },
        )

        reasons = contract["deterministic_gates"]["blocking_reasons"]
        self.assertIn("The target-size search trace belongs to a different stream-budget ledger.", reasons)
        self.assertIn("The target-size search trace belongs to a different production stream plan.", reasons)

    def test_operator_concern_examples_become_structured_tags(self) -> None:
        request = with_quality_risk_intent(
            None,
            note="Faces look soft, motion breaks up, preserve the grain, and keep surround audio.",
        )

        assert request is not None
        self.assertEqual(
            request["quality_risk_tags"],
            [
                "softness_detail_loss",
                "motion_breakup",
                "grain_noise_treatment",
                "audio_quality_layout",
            ],
        )

    def test_rejected_post_test_feedback_is_persisted_with_current_binding(self) -> None:
        saved_states: list[dict[str, object]] = []
        policy = {"video": {"target_vmaf": 90.0}}
        calibration = {
            "job_id": "job-123",
            "sample_result": {"predicted_encode_percent": 12.0},
            "review_moments": [
                {
                    "risk_tags": ["high_motion"],
                    "evidence_id": "ev1_fingerprint",
                }
            ],
        }
        deps = SimpleNamespace(
            load_advice_state=lambda *_args: {
                "quality_risk_records": [
                    self._record(
                        verdict="approved",
                        policy_hash=stable_policy_hash(policy),
                        job_id="job-123",
                        evidence_ids=["ev1_cadence", "ev1_fingerprint"],
                        created_at="2026-03-28T00:00:00+00:00",
                    )
                ]
            },
            save_advice_state=lambda _config, _prefix, payload: saved_states.append(payload),
            now_iso=lambda: "2026-03-29T00:00:00+00:00",
        )

        state = _persist_quality_risk_feedback(
            object(),
            deps,
            prefix="tv/House/Season 2",
            note="I reject this sample because motion breaks up.",
            sample_item=self._sample(),
            current_policy=policy,
            calibration=calibration,
            operator_request={
                "evidence_authority": "rejected_visual_result",
                "quality_risk_tags": ["motion_breakup"],
                "quality_risk_details": "Motion breaks up.",
            },
            latest_failed_sample_job=None,
        )

        self.assertEqual(len(saved_states), 1)
        record = state["quality_risk_records"][-1]
        self.assertEqual(record["verdict"], "rejected")
        self.assertEqual(record["source_id"], "src-house-s2")
        self.assertEqual(record["policy_hash"], stable_policy_hash(policy))
        self.assertEqual(record["sample_job_id"], "job-123")
        self.assertEqual(record["evidence_ids"], ["ev1_cadence", "ev1_fingerprint"])
        self.assertEqual(record["moment_indexes"], [1])
        self.assertEqual(
            state["content_intent_boundary_observation"]["exclusion_reason"],
            "frozen_sample_item_missing",
        )

    def _contract(
            self,
            *,
            policy: dict[str, object],
            records: list[dict[str, object]],
            job_id: str | None = "job-123",
            interpretation: dict[str, object] | None = None,
            sample_result: dict[str, object] | None = None,
    ) -> dict[str, object]:
        sample = self._sample()
        calibration: dict[str, object] = {
            "sample_result": sample_result or {"predicted_encode_percent": 12.0},
            "review_moments": [
                {
                    "timestamp_seconds": 89.0,
                    "duration_seconds": 8.0,
                    "role": "hard",
                    "risk_tags": ["high_motion"],
                    "rationale": "Compare the motion-heavy range.",
                    "confidence": 0.88,
                    "coverage": 0.66,
                    "evidence_id": "ev1_fingerprint",
                }
            ],
        }
        if job_id is not None:
            calibration["job_id"] = job_id
        return build_quality_risk_contract(
            prefix="tv/House/Season 2",
            sample_item=sample,
            current_policy=policy,
            preview_policy=policy,
            calibration=calibration,
            advice_state={"quality_risk_records": records},
            interpretation=interpretation,
        )

    @staticmethod
    def _sample() -> dict[str, object]:
        return {
            "rel_path": "tv/House/Season 2/Episode.mkv",
            "representative_source_id": "src-house-s2",
            "source_fingerprint": "fp-1",
            "stream_budget_ledger": {"feasibility": {"status": "feasible"}},
            "cadence_decision": {
                "classification": "progressive",
                "confidence": 0.98,
                "transform": "none",
                "evidence_id": "ev1_cadence",
            },
            "media_fingerprint_decision": {
                "status": "measured",
                "confidence": 0.88,
                "evidence_id": "ev1_fingerprint",
                "findings": [
                    {
                        "id": "high_motion",
                        "confidence": 0.82,
                        "rationale": "Motion stays elevated in the measured ranges.",
                    }
                ],
            },
        }

    @staticmethod
    def _record(
            *,
            verdict: str,
            policy_hash: str,
            job_id: str,
            evidence_ids: list[str],
            created_at: str,
    ) -> dict[str, object]:
        return {
            "kind": "post_test",
            "verdict": verdict,
            "tags": ["motion_breakup"],
            "details": f"{verdict} result.",
            "prefix": "tv/House/Season 2",
            "source_id": "src-house-s2",
            "policy_hash": policy_hash,
            "sample_job_id": job_id,
            "evidence_ids": evidence_ids,
            "moment_indexes": [1],
            "created_at": created_at,
        }

    @staticmethod
    def _target_size_trace(*, status: str) -> dict[str, object]:
        transform_plan = build_target_size_transform_plan(
            cadence_evidence_id="ev1_cadence",
            cadence_class="progressive",
            cadence_transform="none",
            video_filter="scale=-2:1080:flags=lanczos",
        )
        return {
            "schema_version": 1,
            "status": status,
            "selection_reason": "The size target conflicts with the measured quality floor.",
            "ledger": {"source_id": "src-house-s2"},
            "target": {
                "total_target_bytes": 225_000_000,
                "sample_lower_bound_bytes": 202_500_000,
                "sample_upper_bound_bytes": 247_500_000,
            },
            "quality_floor": {"metric": "VMAF", "minimum": 85.0},
            "crf_bounds": {
                "min_crf": 18,
                "configured_max_crf": 38,
                "search_max_crf": 63,
                "max_crf": 63,
                "range_expanded": True,
                "measured_beyond_configured": True,
                "selected_beyond_configured": False,
            },
            "curve": {"shape": "monotonic", "candidate_count": 4, "max_candidates": 6},
            "transform_plan": transform_plan,
            "selected_candidate": {
                "crf": 31.0,
                "metric": "VMAF",
                "metric_score": 84.0,
                "quality_floor_met": False,
                "predicted_whole_episode_bytes": 225_000_000,
                "within_sample_band": True,
            },
            "candidates": [{"crf": 31.0}],
        }


if __name__ == "__main__":
    unittest.main()
