import json
import unittest
from datetime import UTC, datetime, timedelta

from mediaforce.tuning.quality_memory import QualitySearchContext
from mediaforce.tuning.quality_shadow import (
    MIN_SHADOW_RECOMMENDATIONS,
    QualityShadowRecommendation,
    build_quality_shadow_payload,
    quality_shadow_public_view,
    quality_result_candidate_count,
    quality_result_target_changed,
    quality_shadow_metrics,
    recommend_quality_search_from_rows,
    select_latest_quality_shadow_observation,
)


class QualityShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = datetime(2026, 7, 24, 12, tzinfo=UTC)
        self.context = QualitySearchContext(
            metric="VMAF",
            target=85.0,
            minimum_quality_score=84.0,
            search_objective="quality",
            size_target_bytes=None,
            target_video_bitrate=None,
            source_codec="h264",
            output_width=1920,
            output_height=1080,
            encoder="libsvtav1",
            pixel_format="yuv420p10le",
            preset="4",
            encoder_parameters="tune=0",
            video_filter=None,
            output_container="mkv",
        )

    def test_season_recommendation_uses_only_prior_compatible_observations(self) -> None:
        rows = [
            self._row(index=index, crf=crf)
            for index, crf in enumerate((48.0, 50.0, 52.0, 54.0), start=1)
        ]
        rows.append(
            self._row(
                index=5,
                crf=20.0,
                completed_at=self.as_of + timedelta(minutes=1),
                recorded_at=self.as_of - timedelta(minutes=1),
            )
        )
        rows.append(self._row(index=6, crf=20.0, signature_id="qms1_stale"))
        rows.append(
            self._row(
                index=7,
                crf=20.0,
                completed_at=self.as_of - timedelta(days=10),
                recorded_at=self.as_of + timedelta(minutes=1),
            )
        )
        rows.append(
            self._row(
                index=8,
                crf=20.0,
                signature_id="qms1_unrelated",
                rel_path="tv/Other Show/Season 01/Episode 08.mkv",
            )
        )

        recommendation = recommend_quality_search_from_rows(
            rows,
            source_rel_path="tv/Show/Season 01/Episode 10.mkv",
            source_fingerprint="current-source",
            expected_context=self.context,
            policy_hash="policy-1",
            as_of=self.as_of,
            library_types={"tv": "tv"},
        )

        self.assertTrue(recommendation.available)
        self.assertEqual(recommendation.scope, "season")
        self.assertEqual(recommendation.first_crf, 51.0)
        self.assertEqual(recommendation.sample_count, 4)
        self.assertEqual(recommendation.confidence, "moderate")
        self.assertEqual(len(recommendation.evidence_observation_ids), 4)
        exclusions = {entry.reason: entry.count for entry in recommendation.exclusions}
        self.assertEqual(exclusions["completed_after_cutoff"], 1)
        self.assertEqual(exclusions["recorded_after_cutoff"], 1)
        self.assertEqual(exclusions["stale_signature"], 1)

    def test_non_monotonic_and_conflicting_traces_are_rejected(self) -> None:
        non_monotonic = [
            self._row(
                index=index,
                crf=50.0,
                candidates=[
                    {"crf": 40.0, "metric_score": 85.0},
                    {"crf": 42.0, "metric_score": 86.0},
                ],
            )
            for index in range(1, 5)
        ]
        recommendation = self._recommend(non_monotonic)
        self.assertFalse(recommendation.available)
        self.assertEqual(recommendation.fallback_reason, "non_monotonic_trace")

        conflicting = [
            self._row(
                index=index,
                crf=50.0,
                candidates=[
                    {"crf": 50.0, "metric_score": 83.0},
                    {"crf": 50.0, "metric_score": 85.0},
                ],
            )
            for index in range(1, 5)
        ]
        recommendation = self._recommend(conflicting)
        self.assertFalse(recommendation.available)
        self.assertEqual(recommendation.fallback_reason, "conflicting_quality_evidence")

        target_changed = [
            self._row(
                index=index,
                crf=50.0,
                attempts=[
                    {"metric_target": 86.0, "status": "no_selection"},
                    {"metric_target": 85.0, "status": "selected"},
                ],
            )
            for index in range(1, 5)
        ]
        recommendation = self._recommend(target_changed)
        self.assertFalse(recommendation.available)
        self.assertEqual(recommendation.fallback_reason, "search_target_changed")

    def test_sparse_high_dispersion_and_stale_signature_fallbacks_are_typed(self) -> None:
        sparse = self._recommend([self._row(index=index, crf=50.0) for index in range(1, 4)])
        self.assertEqual(sparse.fallback_reason, "sparse_cohort")

        dispersed = self._recommend(
            [self._row(index=index, crf=crf) for index, crf in enumerate((30.0, 40.0, 50.0, 60.0), start=1)]
        )
        self.assertEqual(dispersed.fallback_reason, "high_dispersion")

        stale = self._recommend(
            [self._row(index=index, crf=50.0, signature_id="qms1_stale") for index in range(1, 7)]
        )
        self.assertEqual(stale.fallback_reason, "stale_signature")

    def test_missing_policy_hash_is_allowed_only_for_staged_backfill(self) -> None:
        runtime_rows = [
            self._row(index=index, crf=50.0, policy_hash=None)
            for index in range(1, 5)
        ]
        self.assertEqual(self._recommend(runtime_rows).fallback_reason, "stale_signature")

        backfill_rows = [
            self._row(
                index=index,
                crf=50.0,
                policy_hash=None,
                authority="staged_backfill",
            )
            for index in range(1, 5)
        ]
        self.assertTrue(self._recommend(backfill_rows).available)

        missing_current_hash = recommend_quality_search_from_rows(
            backfill_rows,
            source_rel_path="tv/Show/Season 01/Episode 10.mkv",
            source_fingerprint="current-source",
            expected_context=self.context,
            policy_hash=None,
            as_of=self.as_of,
            library_types={"tv": "tv"},
        )
        self.assertEqual(missing_current_hash.fallback_reason, "stale_signature")

    def test_pure_rows_resolve_current_authority_before_building_cohorts(self) -> None:
        rows = [
            self._row(index=index, crf=crf)
            for index, crf in enumerate((48.0, 50.0, 52.0, 54.0), start=1)
        ]
        rows.append(
            self._row(
                index=1,
                crf=56.0,
                observation_id="qso1_correction",
                search_run_id="qsr1_1",
                authority="correction",
                revision=1,
            )
        )

        recommendation = self._recommend(rows)

        self.assertEqual(recommendation.sample_count, 4)
        self.assertEqual(recommendation.first_crf, 53.0)
        self.assertIn("qso1_correction", recommendation.evidence_observation_ids)
        self.assertNotIn("qso1_1", recommendation.evidence_observation_ids)

    def test_shadow_comparison_models_hit_and_fallback_cost_without_changing_search(self) -> None:
        recommendation = self._available_recommendation(first_crf=50.0)

        hit = build_quality_shadow_payload(
            recommendation,
            selected_crf=51.0,
            selected_score=85.0,
            minimum_quality_score=84.0,
            candidate_count=5,
            search_duration_seconds=100.0,
            actual_output_bytes=None,
            size_target_bytes=None,
        )
        comparison = hit["comparison"]
        self.assertTrue(comparison["within_one_crf"])
        self.assertEqual(comparison["projected_candidate_count"], 1)
        self.assertEqual(comparison["candidate_savings_rate"], 0.8)
        self.assertEqual(comparison["search_time_savings_rate"], 0.8)
        self.assertFalse(comparison["fallback_needed"])
        self.assertFalse(hit["production_search_changed"])

        miss = build_quality_shadow_payload(
            recommendation,
            selected_crf=53.0,
            selected_score=85.0,
            minimum_quality_score=84.0,
            candidate_count=5,
            search_duration_seconds=100.0,
            actual_output_bytes=None,
            size_target_bytes=None,
        )
        comparison = miss["comparison"]
        self.assertFalse(comparison["within_one_crf"])
        self.assertEqual(comparison["projected_candidate_count"], 6)
        self.assertEqual(comparison["candidate_savings_rate"], -0.2)
        self.assertTrue(comparison["fallback_needed"])
        self.assertTrue(comparison["false_narrow"])

    def test_public_view_separates_measured_run_from_shadow_recommendation(self) -> None:
        recommendation = self._available_recommendation(first_crf=50.0)
        shadow = build_quality_shadow_payload(
            recommendation,
            selected_crf=51.0,
            selected_score=86.5,
            minimum_quality_score=84.0,
            candidate_count=5,
            search_duration_seconds=100.0,
            actual_output_bytes=510_000_000,
            size_target_bytes=500_000_000,
        )
        row = {
            **self._row(index=1, crf=51.0),
            "quality_metric": "VMAF",
            "selected_target": 85.0,
            "selected_score": 86.5,
            "actual_output_bytes": 510_000_000,
            "context_json": json.dumps({"search": {"minimum_quality_score": 84.0}}),
            "shadow_json": json.dumps(shadow),
        }

        public = quality_shadow_public_view(row)

        self.assertIsNotNone(public)
        assert public is not None
        self.assertEqual(public["source_rel_path"], "tv/Show/Season 01/Episode 01.mkv")
        self.assertEqual(
            public["measured"],
            {
                "selected_crf": 51.0,
                "quality_metric": "VMAF",
                "quality_score": 86.5,
                "quality_target": 85.0,
                "quality_floor": 84.0,
                "quality_margin": 2.5,
                "output_bytes": 510_000_000,
                "size_error_percent": 2.0,
                "candidate_count": 5,
                "search_duration_seconds": 100.0,
            },
        )
        self.assertEqual(public["recommendation"]["first_crf"], 50.0)
        self.assertEqual(public["recommendation"]["scope"], "season")
        self.assertEqual(public["recommendation"]["sample_count"], 6)
        self.assertEqual(public["recommendation"]["evidence_count"], 1)
        self.assertEqual(public["comparison"], {"crf_delta": 1.0, "within_one_crf": True})
        self.assertFalse(public["production_search_changed"])

    def test_public_view_keeps_typed_fallback_without_a_recommendation(self) -> None:
        recommendation = QualityShadowRecommendation(
            search_signature_id=self.context.signature_id,
            evidence_cutoff_at=self.as_of.isoformat(),
            first_crf=None,
            scope=None,
            confidence="none",
            sample_count=0,
            cohort_id=None,
            minimum_crf=None,
            maximum_crf=None,
            iqr=None,
            median_absolute_deviation=None,
            evidence_observation_ids=(),
            fallback_reason="sparse_cohort",
            reason="Compatible observations exist, but the cohort is too small.",
            exclusions=(),
        )
        shadow = build_quality_shadow_payload(
            recommendation,
            selected_crf=52.0,
            selected_score=85.0,
            minimum_quality_score=84.0,
            candidate_count=4,
            search_duration_seconds=80.0,
            actual_output_bytes=None,
            size_target_bytes=None,
        )
        public = quality_shadow_public_view(
            {
                **self._row(index=1, crf=52.0),
                "quality_metric": "VMAF",
                "shadow_json": json.dumps(shadow),
            }
        )

        self.assertIsNotNone(public)
        assert public is not None
        self.assertIsNone(public["recommendation"])
        self.assertEqual(public["fallback_reason"], "sparse_cohort")
        self.assertIn("cohort is too small", public["reason"])

    def test_latest_shadow_selection_uses_current_valid_completed_observation(self) -> None:
        shadow = build_quality_shadow_payload(
            self._available_recommendation(first_crf=50.0),
            selected_crf=50.0,
            selected_score=85.0,
            minimum_quality_score=84.0,
            candidate_count=5,
            search_duration_seconds=100.0,
            actual_output_bytes=None,
            size_target_bytes=None,
        )
        older = {
            **self._row(index=2, crf=50.0),
            "shadow_json": json.dumps(shadow),
        }
        newer = {
            **self._row(index=1, crf=51.0),
            "shadow_json": json.dumps(shadow),
        }
        invalid = {
            **self._row(
                index=0,
                crf=52.0,
                completed_at=self.as_of + timedelta(minutes=1),
            ),
            "shadow_json": json.dumps({"algorithm_version": "old"}),
        }

        selected = select_latest_quality_shadow_observation([older, invalid, newer])

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["observation_id"], newer["observation_id"])
        self.assertIsNone(quality_shadow_public_view(None))
        self.assertIsNone(quality_shadow_public_view(invalid))

    def test_metrics_apply_accuracy_savings_and_safety_gates(self) -> None:
        rows = []
        recommendation = self._available_recommendation(first_crf=50.0)
        for index in range(MIN_SHADOW_RECOMMENDATIONS):
            selected_crf = 50.0 if index < 8 else 53.0
            payload = build_quality_shadow_payload(
                recommendation,
                selected_crf=selected_crf,
                selected_score=85.0,
                minimum_quality_score=84.0,
                candidate_count=5,
                search_duration_seconds=100.0,
                actual_output_bytes=None,
                size_target_bytes=None,
            )
            rows.append({"shadow_json": json.dumps(payload)})

        metrics = quality_shadow_metrics(rows)

        self.assertEqual(metrics.evaluated_runs, 10)
        self.assertEqual(metrics.recommendation_runs, 10)
        self.assertEqual(metrics.within_one_rate, 0.8)
        self.assertEqual(metrics.false_narrow_rate, 0.2)
        self.assertEqual(metrics.fallback_need_rate, 0.2)
        self.assertEqual(metrics.median_candidate_savings_rate, 0.8)
        self.assertTrue(metrics.performance_thresholds_met)
        self.assertFalse(metrics.active_eligible)
        self.assertEqual(metrics.blocking_reasons, ("hint_safety_unmeasured",))

        rows.append(
            {
                "outcome_kind": "final_size_failure",
                "timing_json": json.dumps(
                    {"encode_completed_at": (self.as_of + timedelta(minutes=1)).isoformat()}
                ),
                "recorded_at": (self.as_of + timedelta(minutes=1)).isoformat(),
            }
        )
        metrics = quality_shadow_metrics(rows)
        self.assertEqual(metrics.production_final_size_misses, 1)
        self.assertFalse(metrics.performance_thresholds_met)
        self.assertFalse(metrics.active_eligible)
        self.assertIn("production_final_size_miss", metrics.blocking_reasons)

    def test_candidate_count_reads_quality_and_target_size_traces(self) -> None:
        class Result:
            quality_search_trace = {"candidate_count": 7}
            target_size_trace = None

        self.assertEqual(quality_result_candidate_count(Result()), 7)
        Result.quality_search_trace = None
        Result.target_size_trace = {"candidate_count": 3, "candidates": [{}, {}, {}, {}]}
        self.assertEqual(quality_result_candidate_count(Result()), 4)

    def test_relaxed_quality_target_is_not_projected_as_a_one_probe_hit(self) -> None:
        class Result:
            target = 85.0
            target_size_trace = None
            quality_search_trace = {
                "initial_target": 86.0,
                "attempts": [
                    {"metric_target": 86.0, "status": "no_selection"},
                    {"metric_target": 85.0, "status": "selected"},
                ],
            }

        self.assertTrue(quality_result_target_changed(Result()))

    def _recommend(self, rows: list[dict[str, object]]) -> QualityShadowRecommendation:
        return recommend_quality_search_from_rows(
            rows,
            source_rel_path="tv/Show/Season 01/Episode 10.mkv",
            source_fingerprint="current-source",
            expected_context=self.context,
            policy_hash="policy-1",
            as_of=self.as_of,
            library_types={"tv": "tv"},
        )

    def _row(
            self,
            *,
            index: int,
            crf: float,
            completed_at: datetime | None = None,
            recorded_at: datetime | None = None,
            signature_id: str | None = None,
            candidates: list[dict[str, object]] | None = None,
            attempts: list[dict[str, object]] | None = None,
            rel_path: str | None = None,
            policy_hash: str | None = "policy-1",
            authority: str = "runtime_native",
            observation_id: str | None = None,
            search_run_id: str | None = None,
            revision: int = 0,
    ) -> dict[str, object]:
        completion = completed_at or self.as_of - timedelta(days=index)
        recorded = recorded_at or completion
        return {
            "observation_id": observation_id or f"qso1_{index}",
            "search_run_id": search_run_id or f"qsr1_{index}",
            "revision": revision,
            "outcome_kind": "selected",
            "learning_eligible": 1,
            "authority": authority,
            "search_signature_id": signature_id or self.context.signature_id,
            "policy_hash": policy_hash,
            "source_rel_path": rel_path or f"tv/Show/Season 01/Episode {index:02d}.mkv",
            "source_fingerprint": f"source-{index}",
            "selected_crf": crf,
            "selected_target": 85.0,
            "selected_score": 85.0,
            "candidate_trace_json": json.dumps(
                {
                    "attempts": attempts or [],
                    "candidates": candidates or [
                        {"crf": crf - 1, "metric_score": 86.0},
                        {"crf": crf, "metric_score": 85.0},
                    ]
                }
            ),
            "timing_json": json.dumps({"encode_completed_at": completion.isoformat()}),
            "recorded_at": recorded.isoformat(),
        }

    def _available_recommendation(self, *, first_crf: float) -> QualityShadowRecommendation:
        return QualityShadowRecommendation(
            search_signature_id=self.context.signature_id,
            evidence_cutoff_at=self.as_of.isoformat(),
            first_crf=first_crf,
            scope="season",
            confidence="moderate",
            sample_count=6,
            cohort_id="qsc1_test",
            minimum_crf=48.0,
            maximum_crf=52.0,
            iqr=2.0,
            median_absolute_deviation=1.0,
            evidence_observation_ids=("qso1_1",),
            fallback_reason=None,
            reason="test",
            exclusions=(),
        )


if __name__ == "__main__":
    unittest.main()
