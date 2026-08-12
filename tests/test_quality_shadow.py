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

    def test_recommendation_deduplicates_retries_by_item_version(self) -> None:
        rows = [self._row(index=index, crf=50.0) for index in range(1, 4)]
        rows.extend(
            [
                self._row(
                    index=4,
                    crf=40.0,
                    completed_at=self.as_of - timedelta(days=2),
                    rel_path="tv/Show/Season 01/Episode 04.mkv",
                    source_fingerprint="shared-version",
                    observation_id="qso1_retry_old",
                    search_run_id="qsr1_retry_old",
                ),
                self._row(
                    index=5,
                    crf=50.0,
                    completed_at=self.as_of - timedelta(days=1),
                    rel_path="tv/Show/Season 01/Episode 04.mkv",
                    source_fingerprint="shared-version",
                    observation_id="qso1_retry_new",
                    search_run_id="qsr1_retry_new",
                ),
            ]
        )

        recommendation = self._recommend(rows)

        self.assertEqual(recommendation.sample_count, 4)
        self.assertEqual(recommendation.first_crf, 50.0)
        self.assertIn("qso1_retry_new", recommendation.evidence_observation_ids)
        self.assertNotIn("qso1_retry_old", recommendation.evidence_observation_ids)

    def test_final_retry_terminal_is_excluded_from_first_crf_guidance(self) -> None:
        rows = [self._row(index=index, crf=50.0) for index in range(1, 4)]
        rows.append(
            self._row(
                index=4,
                crf=44.0,
                candidates=[
                    {"crf": 46.0, "metric_score": 85.0, "role": "expanded_bound"},
                    {"crf": 44.0, "metric_score": 86.0, "role": "final_retry_measurement"},
                ],
                selected_candidate={
                    "crf": 44.0,
                    "metric_score": 86.0,
                    "role": "final_retry_measurement",
                },
            )
        )

        recommendation = self._recommend(rows)

        self.assertFalse(recommendation.available)
        self.assertEqual(recommendation.fallback_reason, "final_retry_terminal")
        exclusions = {entry.reason: entry.count for entry in recommendation.exclusions}
        self.assertEqual(exclusions["final_retry_terminal"], 1)

    def test_final_retry_terminal_is_excluded_from_passive_accuracy_metrics(self) -> None:
        recommendation = self._available_recommendation(first_crf=44.0)
        payload = build_quality_shadow_payload(
            recommendation,
            selected_crf=42.0,
            selected_score=85.0,
            minimum_quality_score=84.0,
            candidate_count=4,
            search_duration_seconds=100.0,
            actual_output_bytes=300_000_000,
            size_target_bytes=300_000_000,
            selected_candidate_role="final_retry_measurement",
        )

        comparison = payload["comparison"]
        self.assertIsNone(comparison["crf_delta"])
        self.assertIsNone(comparison["within_one_crf"])
        self.assertIsNone(comparison["fallback_needed"])
        self.assertFalse(comparison["false_narrow"])
        self.assertEqual(comparison["evaluation_exclusion_reason"], "final_retry_terminal")

        metrics = quality_shadow_metrics([self._metric_row(index=1, payload=payload)])
        self.assertEqual(metrics.evaluated_runs, 1)
        self.assertEqual(metrics.recommendation_runs, 0)
        self.assertEqual(metrics.fallback_need_rate, 0.0)
        self.assertEqual(dict(metrics.fallback_counts), {"final_retry_terminal": 1})

    def test_final_retry_reason_is_counted_once_per_passive_run(self) -> None:
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
            fallback_reason="final_retry_terminal",
            reason="test",
            exclusions=(),
        )
        payload = build_quality_shadow_payload(
            recommendation,
            selected_crf=42.0,
            selected_score=85.0,
            minimum_quality_score=84.0,
            candidate_count=4,
            search_duration_seconds=100.0,
            actual_output_bytes=300_000_000,
            size_target_bytes=300_000_000,
            selected_candidate_role="final_retry_measurement",
        )

        metrics = quality_shadow_metrics([self._metric_row(index=1, payload=payload)])

        self.assertEqual(dict(metrics.fallback_counts), {"final_retry_terminal": 1})

    def test_newer_final_size_failure_suppresses_older_selected_terminal(self) -> None:
        rows = [self._row(index=index, crf=50.0) for index in range(1, 4)]
        rows.extend(
            [
                self._row(
                    index=4,
                    crf=50.0,
                    completed_at=self.as_of - timedelta(days=2),
                    rel_path="tv/Show/Season 01/Episode 04.mkv",
                    source_fingerprint="shared-version",
                    observation_id="qso1_selected_old",
                    search_run_id="qsr1_selected_old",
                ),
                self._row(
                    index=5,
                    crf=50.0,
                    completed_at=self.as_of - timedelta(days=1),
                    rel_path="tv/Show/Season 01/Episode 04.mkv",
                    source_fingerprint="shared-version",
                    observation_id="qso1_failure_new",
                    search_run_id="qsr1_failure_new",
                    outcome_kind="final_size_failure",
                    learning_eligible=0,
                ),
            ]
        )

        recommendation = self._recommend(rows)

        self.assertFalse(recommendation.available)
        self.assertEqual(recommendation.fallback_reason, "sparse_cohort")

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
            rows.append(
                {
                    "observation_id": f"qso1_metric_{index}",
                    "search_run_id": f"qsr1_metric_{index}",
                    "source_rel_path": f"tv/Show/Season 01/Metric {index}.mkv",
                    "source_fingerprint": f"metric-version-{index}",
                    "search_signature_id": self.context.signature_id,
                    "policy_hash": "policy-test",
                    "outcome_kind": "selected",
                    "recorded_at": f"2026-07-25T12:{index:02d}:00+00:00",
                    "shadow_json": json.dumps(payload),
                }
            )

        metrics = quality_shadow_metrics(rows)

        self.assertEqual(metrics.evaluated_runs, 10)
        self.assertEqual(metrics.recommendation_runs, 10)
        self.assertEqual(metrics.within_one_rate, 0.8)
        self.assertEqual(metrics.false_narrow_rate, 0.2)
        self.assertEqual(metrics.fallback_need_rate, 0.2)
        self.assertEqual(metrics.median_candidate_savings_rate, 0.8)
        self.assertTrue(metrics.performance_thresholds_met)
        self.assertTrue(metrics.active_eligible)

    def test_metrics_deduplicate_retries_by_item_version_signature_and_policy(self) -> None:
        recommendation = self._available_recommendation(first_crf=50.0)
        rows = []
        for index in range(MIN_SHADOW_RECOMMENDATIONS):
            payload = build_quality_shadow_payload(
                recommendation,
                selected_crf=50.0,
                selected_score=85.0,
                minimum_quality_score=84.0,
                candidate_count=5,
                search_duration_seconds=100.0,
                actual_output_bytes=None,
                size_target_bytes=None,
            )
            rows.append(
                {
                    "observation_id": f"qso1_{index}",
                    "search_run_id": f"qsr1_{index}",
                    "source_rel_path": f"tv/Show/Season 01/Episode {index}.mkv",
                    "source_fingerprint": f"version-{index}",
                    "search_signature_id": self.context.signature_id,
                    "policy_hash": "policy-test",
                    "outcome_kind": "selected",
                    "recorded_at": f"2026-07-25T12:{index:02d}:00+00:00",
                    "shadow_json": json.dumps(payload),
                }
            )
        retry = dict(rows[0])
        retry.update({"observation_id": "qso1_retry", "search_run_id": "qsr1_retry", "recorded_at": "2026-07-26T12:00:00+00:00"})
        rows.append(retry)

        metrics = quality_shadow_metrics(rows)

        self.assertEqual(metrics.evaluated_runs, MIN_SHADOW_RECOMMENDATIONS)
        self.assertEqual(metrics.raw_evaluated_runs, MIN_SHADOW_RECOMMENDATIONS + 1)

    def test_metrics_exclude_rows_without_content_version_identity(self) -> None:
        payload = build_quality_shadow_payload(
            self._available_recommendation(first_crf=50.0),
            selected_crf=50.0,
            selected_score=85.0,
            minimum_quality_score=84.0,
            candidate_count=5,
            search_duration_seconds=100.0,
            actual_output_bytes=None,
            size_target_bytes=None,
        )
        rows = [
            {
                "observation_id": f"qso1_unversioned_{index}",
                "search_run_id": f"qsr1_unversioned_{index}",
                "source_rel_path": "tv/Show/Season 01/Episode.mkv",
                "source_fingerprint": None,
                "search_signature_id": self.context.signature_id,
                "policy_hash": "policy-test",
                "outcome_kind": "selected",
                "recorded_at": f"2026-07-25T12:{index:02d}:00+00:00",
                "shadow_json": json.dumps(payload),
            }
            for index in range(MIN_SHADOW_RECOMMENDATIONS)
        ]

        metrics = quality_shadow_metrics(rows)

        self.assertEqual(metrics.raw_evaluated_runs, MIN_SHADOW_RECOMMENDATIONS)
        self.assertEqual(metrics.evaluated_runs, 0)
        self.assertFalse(metrics.active_eligible)

    def test_unversioned_safety_violation_still_blocks_readiness(self) -> None:
        payload = build_quality_shadow_payload(
            self._available_recommendation(first_crf=50.0),
            selected_crf=50.0,
            selected_score=83.0,
            minimum_quality_score=84.0,
            candidate_count=5,
            search_duration_seconds=100.0,
            actual_output_bytes=None,
            size_target_bytes=None,
        )
        metrics = quality_shadow_metrics([
            {
                "observation_id": "qso1_unversioned_safety",
                "search_run_id": "qsr1_unversioned_safety",
                "source_rel_path": "tv/Show/Season 01/Episode.mkv",
                "source_fingerprint": None,
                "search_signature_id": self.context.signature_id,
                "policy_hash": "policy-test",
                "outcome_kind": "selected",
                "recorded_at": "2026-07-25T12:00:00+00:00",
                "shadow_json": json.dumps(payload),
            }
        ])

        self.assertEqual(metrics.evaluated_runs, 0)
        self.assertEqual(metrics.passive_quality_floor_violations, 1)
        self.assertFalse(metrics.active_eligible)
        self.assertIn("production_quality_floor_violation", metrics.blocking_reasons)

    def test_analysis_family_is_derived_without_exact_target_bytes(self) -> None:
        shared_context = {
            "compression_intent_id": "balanced",
            "search_objective": "target_size",
            "source_codec": "h264",
            "encoder": "libsvtav1",
            "preset": "6",
            "pixel_format": "yuv420p10le",
            "output_width": 1920,
            "output_height": 1080,
            "metric": "VMAF",
            "minimum_quality_score": 84.0,
            "encoder_parameters": "film-grain=8:film-grain-denoise=0",
            "output_container": "mkv",
        }
        first = build_quality_shadow_payload(
            self._available_recommendation(first_crf=50.0),
            selected_crf=50.0,
            selected_score=85.0,
            minimum_quality_score=84.0,
            candidate_count=5,
            search_duration_seconds=100.0,
            actual_output_bytes=100_000_000,
            size_target_bytes=100_000_000,
            context={**shared_context, "size_target_bytes": 100_000_000, "target_video_bitrate": 200_000},
        )
        second = build_quality_shadow_payload(
            self._available_recommendation(first_crf=50.0),
            selected_crf=50.0,
            selected_score=85.0,
            minimum_quality_score=84.0,
            candidate_count=5,
            search_duration_seconds=100.0,
            actual_output_bytes=200_000_000,
            size_target_bytes=200_000_000,
            context={**shared_context, "size_target_bytes": 200_000_000, "target_video_bitrate": 400_000},
        )

        self.assertEqual(first["analysis_family_id"], second["analysis_family_id"])
        self.assertTrue(str(first["analysis_family_id"]).startswith("qaf1_"))

    def test_guard_rejected_warm_arm_final_miss_is_baseline_attributable(self) -> None:
        recommendation = self._available_recommendation(first_crf=50.0)
        rows = [
            self._metric_row(
                index=index,
                payload=build_quality_shadow_payload(
                    recommendation,
                    selected_crf=50.0,
                    selected_score=85.0,
                    minimum_quality_score=84.0,
                    candidate_count=5,
                    search_duration_seconds=100.0,
                    actual_output_bytes=None,
                    size_target_bytes=None,
                ),
            )
            for index in range(MIN_SHADOW_RECOMMENDATIONS)
        ]
        rows.append(
            self._metric_row(
                index=MIN_SHADOW_RECOMMENDATIONS,
                outcome_kind="final_size_failure",
                payload=build_quality_shadow_payload(
                    recommendation,
                    selected_crf=50.0,
                    selected_score=85.0,
                    minimum_quality_score=84.0,
                    candidate_count=5,
                    search_duration_seconds=100.0,
                    actual_output_bytes=None,
                    size_target_bytes=None,
                    warm_start_plan={
                        "eligible": True,
                        "experiment_version": "qwa1",
                        "experiment_arm": "warm_start",
                    },
                    warm_start_trace={
                        "status": "guard_rejected",
                        "attempted": False,
                        "fallback_used": False,
                    },
                ),
                outcome_json={
                    "quality_memory_arm": "warm_start",
                    "quality_memory_attempted": False,
                },
            )
        )

        metrics = quality_shadow_metrics(rows)

        self.assertEqual(metrics.active_final_size_misses, 0)
        self.assertEqual(metrics.baseline_final_size_misses, 1)
        self.assertTrue(metrics.active_eligible)

    def test_quality_floor_violation_blocks_active_eligibility(self) -> None:
        recommendation = self._available_recommendation(first_crf=50.0)
        rows = [
            self._metric_row(
                index=index,
                payload=build_quality_shadow_payload(
                    recommendation,
                    selected_crf=50.0,
                    selected_score=83.0 if index == 0 else 85.0,
                    minimum_quality_score=84.0,
                    candidate_count=5,
                    search_duration_seconds=100.0,
                    actual_output_bytes=None,
                    size_target_bytes=None,
                ),
            )
            for index in range(MIN_SHADOW_RECOMMENDATIONS)
        ]

        metrics = quality_shadow_metrics(rows)

        self.assertEqual(metrics.passive_quality_floor_violations, 1)
        self.assertEqual(metrics.active_quality_floor_violations, 0)
        self.assertFalse(metrics.active_eligible)
        self.assertIn("production_quality_floor_violation", metrics.blocking_reasons)

    def test_candidate_count_reads_quality_and_target_size_traces(self) -> None:
        class Result:
            quality_search_trace = {"candidate_count": 7}
            target_size_trace = None

        self.assertEqual(quality_result_candidate_count(Result()), 7)
        Result.quality_search_trace = None
        Result.target_size_trace = {"candidate_count": 3, "candidates": [{}, {}, {}, {}]}
        self.assertEqual(quality_result_candidate_count(Result()), 4)

    def test_active_payload_reports_observed_probe_without_polluting_passive_projection(self) -> None:
        recommendation = self._available_recommendation(first_crf=50.0)
        payload = build_quality_shadow_payload(
            recommendation,
            selected_crf=50.0,
            selected_score=85.0,
            minimum_quality_score=84.0,
            candidate_count=1,
            search_duration_seconds=20.0,
            actual_output_bytes=300_000_000,
            size_target_bytes=300_000_000,
            warm_start_plan={
                "eligible": True,
                "requested_crf": 50.0,
                "candidate_crf": 50,
                "adjusted": False,
                "baseline_median_candidate_count": 5.0,
                "baseline_median_search_seconds": 100.0,
            },
            warm_start_trace={
                "status": "accepted",
                "attempted": True,
                "candidate_count": 1,
                "baseline_candidate_count": 0,
                "total_candidate_count": 1,
                "duration_seconds": 20.0,
                "fallback_used": False,
            },
        )

        self.assertTrue(payload["production_search_changed"])
        self.assertIsNone(payload["comparison"]["candidate_savings_rate"])
        self.assertEqual(payload["warm_start"]["estimated_candidate_savings_rate"], 0.8)
        self.assertEqual(payload["warm_start"]["estimated_search_time_savings_rate"], 0.8)
        public = quality_shadow_public_view(
            {
                "observation_id": "qso1_active",
                "source_rel_path": "tv/Show/Season 01/Episode 01.mkv",
                "recorded_at": self.as_of.isoformat(),
                "selected_crf": 50.0,
                "selected_target": 85.0,
                "selected_score": 85.0,
                "quality_metric": "VMAF",
                "actual_output_bytes": 300_000_000,
                "candidate_count": 1,
                "search_duration_seconds": 20.0,
                "context_json": json.dumps({"search": {"minimum_quality_score": 84.0}}),
                "shadow_json": json.dumps(payload),
            }
        )
        assert public is not None
        self.assertTrue(public["production_search_changed"])
        self.assertEqual(public["warm_start"]["status"], "accepted")
        self.assertEqual(public["warm_start"]["estimated_candidate_savings_rate"], 0.8)

    def test_active_selected_observation_remains_visible_without_becoming_evidence(self) -> None:
        payload = build_quality_shadow_payload(
            self._available_recommendation(first_crf=50.0),
            selected_crf=50.0,
            selected_score=85.0,
            minimum_quality_score=84.0,
            candidate_count=1,
            search_duration_seconds=20.0,
            actual_output_bytes=None,
            size_target_bytes=None,
            warm_start_trace={"status": "accepted", "attempted": True},
        )
        row = {
            "observation_id": "qso1_active",
            "search_run_id": "qsr1_active",
            "revision": 0,
            "authority": "runtime_native",
            "outcome_kind": "selected",
            "learning_eligible": 0,
            "exclusion_reason": "warm_start_selected",
            "timing_json": json.dumps({"encode_completed_at": self.as_of.isoformat()}),
            "shadow_json": json.dumps(payload),
        }

        self.assertIs(select_latest_quality_shadow_observation([row]), row)

    def test_active_rows_do_not_rewrite_passive_readiness_metrics(self) -> None:
        recommendation = self._available_recommendation(first_crf=50.0)
        rows = [
            self._metric_row(
                index=index,
                payload=build_quality_shadow_payload(
                    recommendation,
                    selected_crf=50.0,
                    selected_score=85.0,
                    minimum_quality_score=84.0,
                    candidate_count=5,
                    search_duration_seconds=100.0,
                    actual_output_bytes=None,
                    size_target_bytes=None,
                ),
            )
            for index in range(MIN_SHADOW_RECOMMENDATIONS)
        ]
        rows.append(
            self._metric_row(
                index=MIN_SHADOW_RECOMMENDATIONS,
                payload=build_quality_shadow_payload(
                    recommendation,
                    selected_crf=50.0,
                    selected_score=85.0,
                    minimum_quality_score=84.0,
                    candidate_count=1,
                    search_duration_seconds=20.0,
                    actual_output_bytes=None,
                    size_target_bytes=None,
                    warm_start_trace={"status": "accepted", "attempted": True},
                ),
            )
        )

        metrics = quality_shadow_metrics(rows)

        self.assertEqual(metrics.evaluated_runs, MIN_SHADOW_RECOMMENDATIONS)
        self.assertEqual(metrics.recommendation_runs, MIN_SHADOW_RECOMMENDATIONS)
        self.assertEqual(metrics.median_candidate_savings_rate, 0.8)

    def test_recovered_active_final_size_miss_blocks_future_warm_starts(self) -> None:
        recommendation = self._available_recommendation(first_crf=50.0)
        rows = [
            self._metric_row(
                index=index,
                payload=build_quality_shadow_payload(
                    recommendation,
                    selected_crf=50.0,
                    selected_score=85.0,
                    minimum_quality_score=84.0,
                    candidate_count=5,
                    search_duration_seconds=100.0,
                    actual_output_bytes=None,
                    size_target_bytes=None,
                ),
            )
            for index in range(MIN_SHADOW_RECOMMENDATIONS)
        ]
        rows.append(
            self._metric_row(
                index=MIN_SHADOW_RECOMMENDATIONS,
                payload=build_quality_shadow_payload(
                    recommendation,
                    selected_crf=51.0,
                    selected_score=85.0,
                    minimum_quality_score=84.0,
                    candidate_count=6,
                    search_duration_seconds=120.0,
                    actual_output_bytes=None,
                    size_target_bytes=None,
                    warm_start_trace={
                        "status": "fallback",
                        "attempted": True,
                        "fallback_used": True,
                        "fallback_reason": "final_size_miss",
                    },
                    final_size_miss=True,
                ),
            )
        )

        metrics = quality_shadow_metrics(rows)

        self.assertEqual(metrics.evaluated_runs, MIN_SHADOW_RECOMMENDATIONS)
        self.assertEqual(metrics.active_final_size_misses, 1)
        self.assertEqual(metrics.baseline_final_size_misses, 0)
        self.assertFalse(metrics.active_eligible)
        self.assertIn("active_final_size_miss", metrics.blocking_reasons)

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
            selected_candidate: dict[str, object] | None = None,
            rel_path: str | None = None,
            source_fingerprint: str | None = None,
            policy_hash: str | None = "policy-1",
            authority: str = "runtime_native",
            observation_id: str | None = None,
            search_run_id: str | None = None,
            revision: int = 0,
            outcome_kind: str = "selected",
            learning_eligible: int = 1,
    ) -> dict[str, object]:
        completion = completed_at or self.as_of - timedelta(days=index)
        recorded = recorded_at or completion
        return {
            "observation_id": observation_id or f"qso1_{index}",
            "search_run_id": search_run_id or f"qsr1_{index}",
            "revision": revision,
            "outcome_kind": outcome_kind,
            "learning_eligible": learning_eligible,
            "authority": authority,
            "search_signature_id": signature_id or self.context.signature_id,
            "policy_hash": policy_hash,
            "source_rel_path": rel_path or f"tv/Show/Season 01/Episode {index:02d}.mkv",
            "source_fingerprint": source_fingerprint or f"source-{index}",
            "selected_crf": crf,
            "selected_target": 85.0,
            "selected_score": 85.0,
            "candidate_trace_json": json.dumps(
                {
                    "attempts": attempts or [],
                    "selected_candidate": selected_candidate,
                    "candidates": candidates or [
                        {"crf": crf - 1, "metric_score": 86.0},
                        {"crf": crf, "metric_score": 85.0},
                    ]
                }
            ),
            "timing_json": json.dumps({"encode_completed_at": completion.isoformat()}),
            "recorded_at": recorded.isoformat(),
        }

    def _metric_row(
            self,
            *,
            index: int,
            payload: dict[str, object],
            outcome_kind: str = "selected",
            outcome_json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "observation_id": f"qso1_metric_{index}",
            "search_run_id": f"qsr1_metric_{index}",
            "source_rel_path": f"tv/Show/Season 01/Metric {index}.mkv",
            "source_fingerprint": f"metric-version-{index}",
            "search_signature_id": self.context.signature_id,
            "policy_hash": "policy-test",
            "outcome_kind": outcome_kind,
            "recorded_at": f"2026-07-25T12:{index % 60:02d}:00+00:00",
            "outcome_json": json.dumps(outcome_json or {}),
            "shadow_json": json.dumps(payload),
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
