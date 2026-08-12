import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from mediaforce.encoding.manifest import _planned_quality_observation_context
from mediaforce.encoding.quality_search import QualitySearchPlan
from mediaforce.tuning.quality_memory import QualitySearchContext
from mediaforce.tuning.quality_memory import quality_search_context_from_command
from mediaforce.tuning.quality_shadow import QualityShadowRecommendation, build_quality_shadow_payload
from mediaforce.tuning.quality_warm_start import QualityWarmStartPlan, plan_quality_warm_start


class QualityWarmStartPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        self.context = QualitySearchContext(
            metric="VMAF",
            target=85.0,
            minimum_quality_score=80.0,
            search_objective="target_size",
            size_target_bytes=300_000_000,
            target_video_bitrate=600_000,
            source_codec="h264",
            output_width=1920,
            output_height=1080,
            encoder="libsvtav1",
            pixel_format="yuv420p10le",
            preset="4",
            encoder_parameters="tune=0:film-grain=0:film-grain-denoise=0",
            video_filter=None,
            output_container="mkv",
        )

    def test_qualified_current_policy_cohort_remains_passive_with_a_future_arm(self) -> None:
        rows = self._rows(selected_crf=38.6)

        with patch(
            "mediaforce.tuning.quality_warm_start.load_current_quality_search_observations",
            return_value=rows,
        ) as loader:
            plan = self._plan()

        self.assertFalse(plan.active)
        self.assertTrue(plan.eligible)
        self.assertEqual(plan.experiment_arm, "passive")
        self.assertIn(plan.future_experiment_arm, {"baseline_holdout", "warm_start"})
        self.assertEqual(plan.candidate_crf, 38)
        self.assertTrue(plan.adjusted)
        self.assertEqual(plan.baseline_median_candidate_count, 5.0)
        self.assertEqual(plan.baseline_median_search_seconds, 100.0)
        self.assertIsNone(plan.search_hint())
        self.assertEqual(loader.call_args.kwargs["recorded_before"], self.as_of.isoformat())
        self.assertEqual(loader.call_args.kwargs["scope"].kind, "tv_series")

    def test_baseline_benchmarks_exclude_final_retry_terminals(self) -> None:
        rows = self._rows(selected_crf=30.0)
        for index in range(10):
            final_retry = dict(rows[index])
            final_retry["observation_id"] = f"qso1_final_retry_{index}"
            final_retry["search_run_id"] = f"qsr1_final_retry_{index}"
            final_retry["source_rel_path"] = f"tv/Show/Season 01/Retry {index:02d}.mkv"
            final_retry["source_fingerprint"] = f"retry-source-{index}"
            shadow = json.loads(str(final_retry["shadow_json"]))
            shadow["comparison"]["evaluation_exclusion_reason"] = "final_retry_terminal"
            shadow["comparison"]["candidate_count"] = 50
            shadow["comparison"]["search_duration_seconds"] = 1_000.0
            final_retry["shadow_json"] = json.dumps(shadow)
            rows.append(final_retry)

        plan = self._plan(rows)

        self.assertEqual(plan.readiness.recommendation_runs, 10)
        self.assertEqual(plan.baseline_median_candidate_count, 5.0)
        self.assertEqual(plan.baseline_median_search_seconds, 100.0)

    def test_qualified_holdout_keeps_full_search_authoritative(self) -> None:
        with patch(
            "mediaforce.tuning.quality_warm_start.load_current_quality_search_observations",
            return_value=self._rows(selected_crf=30.0),
        ):
            plan = self._call_plan(search_run_id="qsr1_0")

        self.assertTrue(plan.eligible)
        self.assertFalse(plan.active)
        self.assertEqual(plan.experiment_arm, "passive")
        self.assertIsNone(plan.search_hint())
        payload = plan.to_payload()
        self.assertTrue(payload["eligible"])
        self.assertEqual(payload["experiment_arm"], "passive")
        self.assertEqual(payload["execution_mode"], "passive")
        self.assertEqual(payload["holdout_percent"], 20)

    def test_large_bound_adjustment_blocks_the_hint(self) -> None:
        plan = self._plan(self._rows(selected_crf=45.0))

        self.assertFalse(plan.active)
        self.assertIsNone(plan.search_hint())
        self.assertEqual(plan.candidate_crf, 38)
        self.assertEqual(plan.block_reason, "hint_adjustment_exceeds_one_crf")

    def test_future_assignment_is_stable_across_run_ids_and_changes_with_content_version(self) -> None:
        with patch(
            "mediaforce.tuning.quality_warm_start.load_current_quality_search_observations",
            return_value=self._rows(selected_crf=30.0),
        ):
            first = self._call_plan(search_run_id="qsr1_first")
            second = self._call_plan(search_run_id="qsr1_second")

        self.assertEqual(first.future_experiment_arm, second.future_experiment_arm)
        with patch(
            "mediaforce.tuning.quality_warm_start.load_current_quality_search_observations",
            return_value=self._rows(selected_crf=30.0),
        ):
            arms = {
                plan_quality_warm_start(
                    Mock(),
                    source_rel_path="tv/Show/Season 01/Episode 99.mkv",
                    source_fingerprint=f"content-version-{index}",
                    expected_context=self.context,
                    policy_hash="policy-1",
                    configured_min_crf=18,
                    configured_max_crf=38,
                    search_run_id=f"qsr1_{index}",
                    as_of=self.as_of,
                    library_types={"tv": "tv"},
                ).future_experiment_arm
                for index in range(32)
            }

        self.assertEqual(arms, {"baseline_holdout", "warm_start"})

    def test_analysis_family_cannot_authorize_an_exact_search_hint(self) -> None:
        rows = self._rows(selected_crf=30.0)
        for row in rows:
            row["search_signature_id"] = "qms2_different_exact_signature"
            shadow = json.loads(str(row["shadow_json"]))
            shadow["analysis_family_id"] = "qaf1_shared_reporting_family"
            row["shadow_json"] = json.dumps(shadow)

        plan = self._plan(rows)

        self.assertEqual(plan.block_reason, "recommendation_unavailable")
        self.assertIsNone(plan.search_hint())

    def test_unhashed_backfill_cannot_authorize_active_behavior(self) -> None:
        rows = self._rows(selected_crf=30.0)
        for row in rows:
            row["authority"] = "staged_backfill"
            row["policy_hash"] = None

        plan = self._plan(rows)

        self.assertFalse(plan.active)
        self.assertIsNone(plan.search_hint())
        self.assertEqual(plan.block_reason, "recommendation_unavailable")
        self.assertEqual(plan.recommendation.fallback_reason, "stale_signature")

    def test_passive_threshold_failure_keeps_full_search_authoritative(self) -> None:
        plan = self._plan(self._rows(selected_crf=30.0)[:4])

        self.assertFalse(plan.active)
        self.assertIsNone(plan.search_hint())
        self.assertEqual(plan.block_reason, "shadow_thresholds_not_met")
        self.assertIn("insufficient_shadow_recommendations", plan.readiness.blocking_reasons)

    def test_shadow_gate_remains_the_dominant_blocker_before_hint_clamping(self) -> None:
        plan = self._plan(self._rows(selected_crf=45.0)[:4])

        self.assertFalse(plan.active)
        self.assertEqual(plan.block_reason, "shadow_thresholds_not_met")

    def test_capped_evidence_ids_remain_verifiable_for_large_cohorts(self) -> None:
        rows = self._rows(selected_crf=30.0, count=21)
        for row in rows:
            shadow = json.loads(str(row["shadow_json"]))
            recommendation = shadow["recommendation"]
            recommendation["evidence_observation_ids"] = recommendation["evidence_observation_ids"][:20]
            row["shadow_json"] = json.dumps(shadow)

        plan = self._plan(rows)

        self.assertFalse(plan.active)
        self.assertEqual(plan.readiness.recommendation_runs, 21)
        self.assertEqual(len(plan.recommendation.evidence_observation_ids), 20)
        self.assertEqual(plan.baseline_median_candidate_count, 5.0)

    def test_incomplete_baseline_benchmark_blocks_activation(self) -> None:
        rows = self._rows(selected_crf=30.0)
        for row in rows:
            shadow = json.loads(str(row["shadow_json"]))
            shadow["comparison"]["search_duration_seconds"] = None
            row["shadow_json"] = json.dumps(shadow)

        plan = self._plan(rows)

        self.assertFalse(plan.active)
        self.assertEqual(plan.block_reason, "baseline_benchmark_unavailable")

    def test_baseline_benchmarks_deduplicate_retries_by_content_version(self) -> None:
        rows = self._rows(selected_crf=30.0)
        for index in range(10):
            retry = dict(rows[0])
            retry["observation_id"] = f"qso1_retry_{index}"
            retry["search_run_id"] = f"qsr1_retry_{index}"
            retry["recorded_at"] = (self.as_of - timedelta(seconds=index + 1)).isoformat()
            shadow = json.loads(str(retry["shadow_json"]))
            shadow["comparison"]["candidate_count"] = 50
            shadow["comparison"]["search_duration_seconds"] = 1_000.0
            retry["shadow_json"] = json.dumps(shadow)
            rows.append(retry)

        plan = self._plan(rows)

        self.assertEqual(plan.readiness.recommendation_runs, 10)
        self.assertIsNone(plan.block_reason)
        self.assertEqual(plan.baseline_median_candidate_count, 5.0)
        self.assertEqual(plan.baseline_median_search_seconds, 100.0)

    def test_readiness_uses_only_rows_from_the_selected_recommendation_scope(self) -> None:
        rows = self._rows(selected_crf=30.0)
        for row in rows[:5]:
            shadow = json.loads(str(row["shadow_json"]))
            shadow["recommendation"]["scope"] = "item"
            row["shadow_json"] = json.dumps(shadow)

        plan = self._plan(rows)

        self.assertEqual(plan.recommendation.scope, "season")
        self.assertEqual(plan.readiness.recommendation_runs, 5)
        self.assertEqual(plan.block_reason, "shadow_thresholds_not_met")

    def test_presearch_signature_matches_the_final_command_signature(self) -> None:
        video_policy = {
            "encoder": "libsvtav1",
            "pixel_format": "yuv420p10le",
        }
        search_plan = QualitySearchPlan(
            host=None,
            metric_name="vmaf",
            metric_target=85.0,
            min_metric_score=80.0,
            relax_step=0.5,
            svt_params=["tune=0", "film-grain=0", "film-grain-denoise=0"],
            preset=4,
            video_filter=None,
        )
        planned = _planned_quality_observation_context(
            search_plan=search_plan,
            video_policy=video_policy,
            item={"video_codec": "h264", "duration_seconds": 3600.0},
            stream_budget=None,
            output_container=".mkv",
            width=1920,
            height=1080,
            detected_crop=None,
        )
        command = [
            "ffmpeg",
            "-c:v",
            "libsvtav1",
            "-pix_fmt",
            "yuv420p10le",
            "-preset",
            "4",
            "-svtav1-params",
            "tune=0:film-grain=0:film-grain-denoise=0",
            "/tmp/output.mkv",
        ]
        final = quality_search_context_from_command(
            command,
            metric="VMAF",
            target=85.0,
            minimum_quality_score=80.0,
            search_objective="quality",
            size_target_bytes=None,
            target_video_bitrate=None,
            source_codec="h264",
            output_width=1920,
            output_height=1080,
        )

        assert planned is not None
        self.assertEqual(planned.signature_id, final.signature_id)

    def test_presearch_signature_resolves_ffmpeg_scale_width(self) -> None:
        planned = _planned_quality_observation_context(
            search_plan=QualitySearchPlan(
                host=None,
                metric_name="vmaf",
                metric_target=85.0,
                min_metric_score=80.0,
                relax_step=0.5,
                svt_params=["tune=0"],
                preset=4,
                video_filter="scale=-2:720:flags=lanczos",
            ),
            video_policy={
                "encoder": "libsvtav1",
                "pixel_format": "yuv420p10le",
                "max_height": 720,
            },
            item={"video_codec": "h264", "duration_seconds": 3600.0},
            stream_budget=None,
            output_container=".mkv",
            width=1920,
            height=1080,
            detected_crop=None,
        )

        assert planned is not None
        self.assertEqual(planned.output_width, 1280)
        self.assertEqual(planned.output_height, 720)

    def _plan(self, rows: list[dict[str, object]] | None = None) -> QualityWarmStartPlan:
        if rows is None:
            return self._call_plan()
        with patch(
            "mediaforce.tuning.quality_warm_start.load_current_quality_search_observations",
            return_value=rows,
        ):
            return self._call_plan()

    def _call_plan(self, *, search_run_id: str = "qsr1_1") -> QualityWarmStartPlan:
        return plan_quality_warm_start(
            Mock(),
            source_rel_path="tv/Show/Season 01/Episode 99.mkv",
            source_fingerprint="current-source",
            expected_context=self.context,
            policy_hash="policy-1",
            configured_min_crf=18,
            configured_max_crf=38,
            search_run_id=search_run_id,
            as_of=self.as_of,
            library_types={"tv": "tv"},
        )

    def _rows(self, *, selected_crf: float, count: int = 10) -> list[dict[str, object]]:
        observation_ids = tuple(f"qso1_{index}" for index in range(count))
        recommendation = QualityShadowRecommendation(
            search_signature_id=self.context.signature_id,
            evidence_cutoff_at=self.as_of.isoformat(),
            first_crf=selected_crf,
            scope="season",
            confidence="high",
            sample_count=len(observation_ids),
            cohort_id="qsc1_test",
            minimum_crf=selected_crf,
            maximum_crf=selected_crf,
            iqr=0.0,
            median_absolute_deviation=0.0,
            evidence_observation_ids=observation_ids,
            fallback_reason=None,
            reason="test",
            exclusions=(),
        )
        rows: list[dict[str, object]] = []
        for index, observation_id in enumerate(observation_ids):
            completed_at = self.as_of - timedelta(minutes=index + 1)
            shadow = build_quality_shadow_payload(
                recommendation,
                selected_crf=selected_crf,
                selected_score=85.0,
                minimum_quality_score=80.0,
                candidate_count=5,
                search_duration_seconds=100.0,
                actual_output_bytes=300_000_000,
                size_target_bytes=300_000_000,
            )
            rows.append(
                {
                    "observation_id": observation_id,
                    "search_run_id": f"qsr1_{index}",
                    "revision": 0,
                    "outcome_kind": "selected",
                    "learning_eligible": 1,
                    "authority": "runtime_native",
                    "search_signature_id": self.context.signature_id,
                    "policy_hash": "policy-1",
                    "search_objective": "target_size",
                    "quality_metric": "VMAF",
                    "source_rel_path": f"tv/Show/Season 01/Episode {index:02d}.mkv",
                    "source_fingerprint": f"source-{index}",
                    "selected_crf": selected_crf,
                    "selected_target": 85.0,
                    "selected_score": 85.0,
                    "candidate_trace_json": json.dumps(
                        {
                            "candidates": [
                                {"crf": selected_crf - 1, "metric_score": 86.0},
                                {"crf": selected_crf, "metric_score": 85.0},
                            ]
                        }
                    ),
                    "timing_json": json.dumps({"encode_completed_at": completed_at.isoformat()}),
                    "recorded_at": completed_at.isoformat(),
                    "shadow_json": json.dumps(shadow),
                }
            )
        return rows


if __name__ == "__main__":
    unittest.main()
