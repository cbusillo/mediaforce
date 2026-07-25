import unittest
from pathlib import Path
from statistics import median
from typing import Any

from mediaforce.encoding.quality import QualitySearchResult, QualitySearchWarmStart, SampleEncodeError, SampleEncodeResult
from mediaforce.tuning.size_goals import SizeGoalIntent
from mediaforce.tuning.stream_budget import StreamBudgetLedger, resolve_stream_budget_ledger
from mediaforce.tuning.target_size_search import (
    TargetSizeSearchError,
    build_target_size_transform_plan,
    retry_quality_result_for_final_miss,
    search_target_size,
    verify_final_output_size,
)


class TargetSizeSearchTests(unittest.TestCase):
    def test_trusted_hint_can_satisfy_the_existing_selector_in_one_probe(self) -> None:
        ledger = self._ledger(target_bytes=300_000_000, source_size_bytes=1_000_000_000)
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

        result = search_target_size(
            Path("/tmp/source.mkv"),
            self._policy(),
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
            video_filter="scale=-2:1080:flags=lanczos",
            stream_budget_ledger=ledger,
            transform_plan=self._transform_plan(video_filter="scale=-2:1080:flags=lanczos"),
            process_controller=None,
            host=None,
            quality_temp_dir=None,
            run_sample_encode=run_sample,
            warm_start=QualitySearchWarmStart(26.0, 26, "qms1_test", "qsc1_test"),
            expected_search_signature_id="qms1_test",
        )

        self.assertEqual(result.crf, 26.0)
        self.assertEqual(seen, [26])
        trace = result.target_size_trace or {}
        self.assertEqual(trace["candidate_count"], 1)
        self.assertEqual(trace["warm_start"]["status"], "accepted")
        self.assertEqual(trace["candidates"][0]["role"], "quality_memory_hint")

    def test_rejected_hint_is_discarded_before_the_full_baseline_search(self) -> None:
        ledger = self._ledger(target_bytes=300_000_000, source_size_bytes=1_000_000_000)

        def run(warm_start: QualitySearchWarmStart | None) -> tuple[QualitySearchResult, list[int]]:
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

            return search_target_size(
                Path("/tmp/source.mkv"),
                self._policy(),
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
                stream_budget_ledger=ledger,
                transform_plan=self._transform_plan(),
                process_controller=None,
                host=None,
                quality_temp_dir=None,
                run_sample_encode=run_sample,
                warm_start=warm_start,
                expected_search_signature_id="qms1_test" if warm_start is not None else None,
            ), seen

        baseline, baseline_seen = run(None)
        warmed, warmed_seen = run(QualitySearchWarmStart(20.0, 20, "qms1_test", "qsc1_test"))

        self.assertEqual(warmed.crf, baseline.crf)
        self.assertEqual(baseline_seen, [31, 18, 24, 28, 26])
        self.assertEqual(warmed_seen, [20, *baseline_seen])
        trace = warmed.target_size_trace or {}
        self.assertEqual(
            [candidate["crf"] for candidate in trace["candidates"]],
            [candidate["crf"] for candidate in (baseline.target_size_trace or {})["candidates"]],
        )
        self.assertEqual(trace["warm_start"]["status"], "rejected_fallback")
        self.assertEqual(trace["warm_start"]["baseline_candidate_count"], len(baseline_seen))
        self.assertEqual(trace["candidate_count"], len(warmed_seen))

    def test_failed_launched_hint_probe_counts_as_work_before_baseline_search(self) -> None:
        ledger = self._ledger(target_bytes=300_000_000, source_size_bytes=1_000_000_000)
        seen: list[int] = []

        def run_sample(_source_path: Path, *, crf: float, **_: Any) -> SampleEncodeResult:
            crf_int = int(crf)
            seen.append(crf_int)
            if len(seen) == 1:
                raise SampleEncodeError("probe failed")
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

        result = search_target_size(
            Path("/tmp/source.mkv"),
            self._policy(),
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
            stream_budget_ledger=ledger,
            transform_plan=self._transform_plan(),
            process_controller=None,
            host=None,
            quality_temp_dir=None,
            run_sample_encode=run_sample,
            warm_start=QualitySearchWarmStart(20.0, 20, "qms1_test", "qsc1_test"),
            expected_search_signature_id="qms1_test",
        )

        trace = result.target_size_trace or {}
        self.assertEqual(seen, [20, 31, 18, 24, 28, 26])
        self.assertEqual(trace["candidate_count"], len(seen))
        self.assertEqual(trace["warm_start"]["candidate_count"], 1)
        self.assertEqual(trace["warm_start"]["status"], "probe_error_fallback")

    def test_stale_target_size_signature_is_rejected_before_measurement(self) -> None:
        ledger = self._ledger(target_bytes=300_000_000, source_size_bytes=1_000_000_000)
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

        result = search_target_size(
            Path("/tmp/source.mkv"),
            self._policy(),
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
            stream_budget_ledger=ledger,
            transform_plan=self._transform_plan(),
            process_controller=None,
            host=None,
            quality_temp_dir=None,
            run_sample_encode=run_sample,
            warm_start=QualitySearchWarmStart(20.0, 20, "qms1_stale", "qsc1_test"),
            expected_search_signature_id="qms1_test",
        )

        trace = result.target_size_trace or {}
        self.assertEqual(seen, [31, 18, 24, 28, 26])
        self.assertEqual(trace["warm_start"]["status"], "guard_rejected")
        self.assertEqual(trace["warm_start"]["candidate_count"], 0)
        self.assertEqual(trace["candidate_count"], len(seen))

    def test_qualified_repeat_corpus_reduces_median_candidate_work_and_search_time(self) -> None:
        candidate_savings: list[float] = []
        time_savings: list[float] = []
        for item_index in range(30):
            ledger = self._ledger(target_bytes=300_000_000, source_size_bytes=1_000_000_000)

            def run(warm_start: QualitySearchWarmStart | None) -> tuple[QualitySearchResult, int, float]:
                calls = 0
                elapsed_seconds = 0.0

                def run_sample(_source_path: Path, *, crf: float, **_: Any) -> SampleEncodeResult:
                    nonlocal calls, elapsed_seconds
                    calls += 1
                    elapsed_seconds += 10.0 + item_index / 100
                    crf_int = int(crf)
                    predicted_video_bytes = 1_000_000_000 - 28_000_000 * crf_int
                    return SampleEncodeResult(
                        "VMAF",
                        88.0 - (crf_int - 18) * 0.2,
                        predicted_video_bytes / 10_000_000,
                        10.0 + item_index / 100,
                        predicted_video_bytes,
                        f"crf {crf_int}",
                        crf_int * 100_000,
                    )

                result = search_target_size(
                    Path(f"/tmp/source-{item_index}.mkv"),
                    self._policy(),
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
                    stream_budget_ledger=ledger,
                    transform_plan=self._transform_plan(),
                    process_controller=None,
                    host=None,
                    quality_temp_dir=None,
                    run_sample_encode=run_sample,
                    warm_start=warm_start,
                    expected_search_signature_id="qms1_test" if warm_start is not None else None,
                )
                return result, calls, elapsed_seconds

            baseline, baseline_calls, baseline_seconds = run(None)
            warmed, warmed_calls, warmed_seconds = run(
                QualitySearchWarmStart(26.0, 26, "qms1_test", "qsc1_test")
            )
            self.assertEqual(warmed.crf, baseline.crf)
            self.assertGreaterEqual(warmed.score, 80.0)
            candidate_savings.append((baseline_calls - warmed_calls) / baseline_calls)
            time_savings.append((baseline_seconds - warmed_seconds) / baseline_seconds)

        self.assertGreaterEqual(median(candidate_savings), 0.20)
        self.assertGreaterEqual(median(time_savings), 0.20)

    def test_monotonic_curve_selects_candidate_inside_sample_target_band(self) -> None:
        ledger = self._ledger(target_bytes=300_000_000, source_size_bytes=1_000_000_000)
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

        result = search_target_size(
            Path("/tmp/source.mkv"),
            self._policy(),
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
            video_filter="scale=-2:1080:flags=lanczos",
            stream_budget_ledger=ledger,
            transform_plan=self._transform_plan(video_filter="scale=-2:1080:flags=lanczos"),
            process_controller=None,
            host=None,
            quality_temp_dir=None,
            run_sample_encode=run_sample,
        )

        self.assertEqual(result.crf, 26.0)
        self.assertEqual(seen, [31, 18, 24, 28, 26])
        trace = result.target_size_trace or {}
        self.assertEqual(trace["status"], "selected")
        self.assertEqual(trace["target"]["total_target_bytes"], 300_000_000)
        self.assertEqual(trace["selected_candidate"]["sampled_clip_bytes"], 2_600_000)
        self.assertEqual(trace["selected_candidate"]["predicted_video_bytes"], 272_000_000)
        self.assertEqual(trace["selected_candidate"]["predicted_whole_episode_bytes"], 276_000_000)
        self.assertEqual(trace["transform_plan"]["cadence_transform"], "none")

    def test_quality_floor_conflict_reports_target_best_reachable_and_floor(self) -> None:
        ledger = self._ledger(target_bytes=300_000_000, source_size_bytes=1_000_000_000)

        def run_sample(_path: Path, *, crf: float, **_kwargs: Any) -> SampleEncodeResult:
            crf_int = int(crf)
            predicted_video_bytes = 1_000_000_000 - 28_000_000 * crf_int
            score = 79.5 if crf_int >= 25 else 82.0
            return SampleEncodeResult("VMAF", score, predicted_video_bytes / 10_000_000, 30.0, predicted_video_bytes, "")

        with self.assertRaises(TargetSizeSearchError) as context:
            search_target_size(
                Path("/tmp/source.mkv"),
                self._policy(),
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
                stream_budget_ledger=ledger,
                transform_plan=self._transform_plan(),
                process_controller=None,
                host=None,
                quality_temp_dir=None,
                run_sample_encode=run_sample,
            )

        self.assertEqual(context.exception.status, "quality_conflict")
        trace = context.exception.trace
        self.assertEqual(trace["target"]["total_target_bytes"], 300_000_000)
        self.assertEqual(trace["quality_floor"]["minimum"], 80.0)
        self.assertEqual(trace["best_reachable_candidate"]["predicted_whole_episode_bytes"], 332_000_000)
        self.assertIn("quality floor", str(context.exception).lower())

    def test_source_cap_below_target_band_reports_bound_exhaustion_without_sampling(self) -> None:
        ledger = self._ledger(target_bytes=300_000_000, source_size_bytes=1_000_000_000, max_encoded_percent=20)
        calls = 0

        def run_sample(_source_path: Path, *, crf: float, **_: Any) -> SampleEncodeResult:
            nonlocal calls
            calls += 1
            return SampleEncodeResult("VMAF", 90.0, 20.0, 30.0, 200_000_000, "")

        with self.assertRaises(TargetSizeSearchError) as context:
            search_target_size(
                Path("/tmp/source.mkv"),
                self._policy(max_encoded_percent=20),
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
                stream_budget_ledger=ledger,
                transform_plan=self._transform_plan(),
                process_controller=None,
                host=None,
                quality_temp_dir=None,
                run_sample_encode=run_sample,
                warm_start=QualitySearchWarmStart(26.0, 26, "qms1_test", "qsc1_test"),
                expected_search_signature_id="qms1_test",
            )

        self.assertEqual(calls, 0)
        self.assertEqual(context.exception.status, "bound_exhausted")
        self.assertEqual(context.exception.trace["selection_reason"], "target_lower_bound_exceeds_source_relative_cap")

    def test_size_search_expands_beyond_configured_crf_range_when_measurements_require_it(self) -> None:
        ledger = self._ledger(target_bytes=225_000_000, source_size_bytes=3_000_000_000)
        seen: list[int] = []

        def run_sample(_source_path: Path, *, crf: float, **_: Any) -> SampleEncodeResult:
            crf_int = int(crf)
            seen.append(crf_int)
            predicted_video_bytes = max(50_000_000, 2_025_000_000 - 40_000_000 * crf_int)
            return SampleEncodeResult(
                "VMAF",
                94.0 - max(0, crf_int - 38) * 0.2,
                predicted_video_bytes / 30_000_000,
                30.0,
                predicted_video_bytes,
                f"crf {crf_int}",
            )

        result = search_target_size(
            Path("/tmp/source.mkv"),
            self._policy(),
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
            search_max_crf=63,
            svt_params=[],
            video_filter=None,
            stream_budget_ledger=ledger,
            transform_plan=self._transform_plan(),
            process_controller=None,
            host=None,
            quality_temp_dir=None,
            run_sample_encode=run_sample,
        )

        self.assertEqual(result.crf, 45.0)
        self.assertIn(45, seen)
        trace = result.target_size_trace or {}
        self.assertEqual(trace["crf_bounds"]["configured_max_crf"], 38)
        self.assertEqual(trace["crf_bounds"]["search_max_crf"], 63)
        self.assertTrue(trace["crf_bounds"]["range_expanded"])
        self.assertTrue(trace["crf_bounds"]["measured_beyond_configured"])
        self.assertTrue(trace["crf_bounds"]["selected_beyond_configured"])

    def test_legacy_target_search_without_explicit_ceiling_preserves_saved_bound(self) -> None:
        ledger = self._ledger(target_bytes=225_000_000, source_size_bytes=3_000_000_000)
        seen: list[int] = []

        with self.assertRaises(TargetSizeSearchError) as context:
            search_target_size(
                Path("/tmp/source.mkv"),
                self._policy(),
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
                stream_budget_ledger=ledger,
                transform_plan=self._transform_plan(),
                process_controller=None,
                host=None,
                quality_temp_dir=None,
                run_sample_encode=lambda _path, *, crf, **_kwargs: (
                    seen.append(int(crf))
                    or SampleEncodeResult("VMAF", 92.0, 50.0, 30.0, 500_000_000, "")
                ),
            )

        self.assertEqual(context.exception.status, "bound_exhausted")
        self.assertLessEqual(max(seen), 38)
        self.assertFalse(context.exception.trace["crf_bounds"]["range_expanded"])

    def test_arithmetic_impossible_budget_reports_structured_infeasibility(self) -> None:
        ledger = self._ledger(target_bytes=3_000_000, source_size_bytes=1_000_000_000)
        with self.assertRaises(TargetSizeSearchError) as context:
            search_target_size(
                Path("/tmp/source.mkv"),
                self._policy(),
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
                stream_budget_ledger=ledger,
                transform_plan=self._transform_plan(),
                process_controller=None,
                host=None,
                quality_temp_dir=None,
                run_sample_encode=lambda _path, *, crf, **_kwargs: SampleEncodeResult(
                    "VMAF", 90.0, 1.0, 30.0, 1, ""
                ),
            )

        self.assertEqual(context.exception.status, "infeasible")
        self.assertEqual(context.exception.trace["selection_reason"], "arithmetically_infeasible_stream_budget")
        self.assertEqual(context.exception.trace["ledger"]["feasibility_status"], "arithmetically_infeasible")

    def test_non_monotonic_curve_exhausts_bounded_search_as_needs_review(self) -> None:
        ledger = self._ledger(target_bytes=300_000_000, source_size_bytes=1_000_000_000)
        sizes = {
            18: 500_000_000,
            24: 360_000_000,
            28: 370_000_000,
            30: 380_000_000,
            31: 100_000_000,
            38: 400_000_000,
        }

        with self.assertRaises(TargetSizeSearchError) as context:
            search_target_size(
                Path("/tmp/source.mkv"),
                self._policy(),
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
                stream_budget_ledger=ledger,
                transform_plan=self._transform_plan(),
                process_controller=None,
                host=None,
                quality_temp_dir=None,
                run_sample_encode=lambda _path, *, crf, **_kwargs: SampleEncodeResult(
                    "VMAF", 90.0, 25.0, 30.0, sizes[int(crf)], ""
                ),
            )

        self.assertEqual(context.exception.status, "needs_review")
        self.assertEqual(context.exception.trace["curve"]["shape"], "non_monotonic")
        self.assertEqual(len(context.exception.trace["candidates"]), 6)

    def test_final_output_verification_allows_only_one_calibrated_retry(self) -> None:
        ledger = self._ledger(target_bytes=300_000_000, source_size_bytes=1_000_000_000)
        verification = verify_final_output_size(ledger, 330_000_000, retry_count=0)
        self.assertEqual(verification.status, "over_target")
        self.assertTrue(verification.retry_allowed)

        quality = search_target_size(
            Path("/tmp/source.mkv"),
            self._policy(),
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
            stream_budget_ledger=ledger,
            transform_plan=self._transform_plan(),
            process_controller=None,
            host=None,
            quality_temp_dir=None,
            run_sample_encode=lambda _path, *, crf, **_kwargs: SampleEncodeResult(
                "VMAF",
                90.0,
                (1_000_000_000 - 28_000_000 * int(crf)) / 10_000_000,
                30.0,
                1_000_000_000 - 28_000_000 * int(crf),
                "",
            ),
        )
        measured_crfs: list[float] = []

        def measure_candidate(crf: float) -> SampleEncodeResult:
            measured_crfs.append(crf)
            predicted_video_bytes = 1_000_000_000 - 28_000_000 * int(crf)
            return SampleEncodeResult(
                "VMAF",
                90.0,
                predicted_video_bytes / 10_000_000,
                30.0,
                predicted_video_bytes,
                "",
            )

        retry_quality = retry_quality_result_for_final_miss(
            quality,
            verification,
            measure_candidate=measure_candidate,
        )

        self.assertIsNotNone(retry_quality)
        assert retry_quality is not None
        self.assertEqual(measured_crfs, [27.0])
        self.assertEqual(retry_quality.crf, 27.0)
        retry_curve = retry_quality.target_size_trace["curve"]
        self.assertEqual(retry_curve["candidate_count"], retry_curve["search_candidate_count"] + 1)
        self.assertEqual(retry_curve["final_retry_measurement_count"], 1)

        baseline_trace = dict(quality.target_size_trace or {})
        baseline_candidate_count = max(
            int(baseline_trace.get("candidate_count", 0)),
            len(baseline_trace.get("candidates", [])),
        )
        baseline_trace["candidate_count"] = baseline_candidate_count + 1
        baseline_trace["warm_start"] = {
            "status": "rejected_fallback",
            "attempted": True,
            "candidate_count": 1,
            "baseline_candidate_count": baseline_candidate_count,
            "total_candidate_count": baseline_candidate_count + 1,
        }
        warm_quality = QualitySearchResult(
            crf=quality.crf,
            metric=quality.metric,
            target=quality.target,
            score=quality.score,
            stdout=quality.stdout,
            target_size_trace=baseline_trace,
        )
        warm_retry = retry_quality_result_for_final_miss(
            warm_quality,
            verification,
            measure_candidate=measure_candidate,
        )

        assert warm_retry is not None
        warm_retry_trace = warm_retry.target_size_trace or {}
        self.assertEqual(warm_retry_trace["candidate_count"], baseline_candidate_count + 2)
        self.assertEqual(warm_retry_trace["warm_start"]["baseline_candidate_count"], baseline_candidate_count + 1)
        self.assertEqual(warm_retry_trace["warm_start"]["total_candidate_count"], baseline_candidate_count + 2)
        exhausted = verify_final_output_size(ledger, 330_000_000, retry_count=1)
        self.assertFalse(exhausted.retry_allowed)
        self.assertIsNone(retry_quality_result_for_final_miss(quality, exhausted, measure_candidate=measure_candidate))
        self.assertEqual(measured_crfs, [27.0, 27.0])

    def test_final_output_retry_measures_interpolated_crf_from_actual_output(self) -> None:
        target_size_bytes = 374_403_556
        non_video_bytes = 66_775_402
        selected = {
            "attempt": 3,
            "role": "refine",
            "crf": 34.0,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 93.9602279663086,
            "quality_floor_met": True,
            "predicted_video_bytes": 273_124_145,
            "predicted_whole_episode_bytes": 339_899_547,
            "violates_source_cap": False,
        }
        bracket = {
            "attempt": 2,
            "role": "refine",
            "crf": 38.0,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 92.91368865966797,
            "quality_floor_met": True,
            "predicted_video_bytes": 216_536_877,
            "predicted_whole_episode_bytes": 283_312_279,
            "violates_source_cap": False,
        }
        quality = QualitySearchResult(
            crf=34.0,
            metric="VMAF",
            target=85.0,
            score=93.9602279663086,
            stdout="target-size-search",
            target_size_trace={
                "schema_version": 1,
                "status": "selected",
                "target": {
                    "total_target_bytes": target_size_bytes,
                    "non_video_bytes": non_video_bytes,
                    "sample_projection_tolerance_percent": 10.0,
                    "final_output_tolerance_percent": 5.0,
                },
                "source_cap": {"video_cap_bytes": 649_576_742},
                "quality_floor": {"metric": "VMAF", "target": 85.0, "minimum": 80.0},
                "candidates": [bracket, selected],
                "selected_candidate": selected,
            },
        )
        ledger = self._ledger(target_bytes=target_size_bytes, source_size_bytes=895_440_181)
        verification = verify_final_output_size(ledger, 406_928_998, retry_count=0)
        measured_crfs: list[float] = []

        def measure_candidate(crf: float) -> SampleEncodeResult:
            measured_crfs.append(crf)
            return SampleEncodeResult(
                metric="VMAF",
                score=93.41461181640625,
                predicted_encode_percent=30.53648895336881,
                predicted_encode_seconds=7562.0,
                predicted_encode_size_bytes=240_554_519,
                stdout="crf 36 measured",
            )

        retry_quality = retry_quality_result_for_final_miss(
            quality,
            verification,
            measure_candidate=measure_candidate,
        )

        self.assertIsNotNone(retry_quality)
        assert retry_quality is not None
        self.assertEqual(measured_crfs, [36.0])
        self.assertEqual(retry_quality.crf, 36.0)
        self.assertEqual(retry_quality.score, 93.41461181640625)
        retry_trace = retry_quality.target_size_trace or {}
        self.assertEqual(retry_trace["selection_reason"], "calibrated_final_size_retry_candidate")
        self.assertEqual(
            retry_trace["final_retry_calibration"]["calibrated_predicted_whole_episode_bytes"],
            366_366_215,
        )
        self.assertEqual(retry_trace["selected_candidate"]["role"], "final_retry_measurement")

    def test_final_output_retry_rejects_interpolated_sample_below_quality_floor(self) -> None:
        selected = {
            "crf": 30.0,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 86.0,
            "quality_floor_met": True,
            "predicted_video_bytes": 1_000_000,
            "predicted_whole_episode_bytes": 5_000_000,
            "violates_source_cap": False,
        }
        bracket = {
            "crf": 34.0,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 82.0,
            "quality_floor_met": True,
            "predicted_video_bytes": 200_000,
            "predicted_whole_episode_bytes": 4_200_000,
            "violates_source_cap": False,
        }
        quality = QualitySearchResult(
            crf=30.0,
            metric="VMAF",
            target=85.0,
            score=86.0,
            stdout="target-size-search",
            target_size_trace={
                "target": {
                    "total_target_bytes": 5_000_000,
                    "non_video_bytes": 4_000_000,
                    "sample_projection_tolerance_percent": 10.0,
                },
                "source_cap": {"video_cap_bytes": 10_000_000},
                "quality_floor": {"metric": "VMAF", "target": 85.0, "minimum": 80.0},
                "candidates": [selected, bracket],
                "selected_candidate": selected,
            },
        )
        verification = verify_final_output_size(
            self._ledger(target_bytes=5_000_000, source_size_bytes=20_000_000),
            5_400_000,
            retry_count=0,
        )

        retry_quality = retry_quality_result_for_final_miss(
            quality,
            verification,
            measure_candidate=lambda _crf: SampleEncodeResult(
                "VMAF", 79.9, 20.0, 30.0, 800_000, "below floor"
            ),
        )

        self.assertIsNone(retry_quality)
        rejection_trace = quality.target_size_trace or {}
        self.assertEqual(rejection_trace["selection_reason"], "final_retry_measurement_below_quality_floor")
        self.assertEqual(rejection_trace["final_retry_calibration"]["status"], "rejected")
        self.assertEqual(rejection_trace["candidates"][-1]["role"], "final_retry_measurement")

    def test_final_output_retry_interpolates_lower_crf_for_under_target_output(self) -> None:
        selected = {
            "crf": 34.0,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 86.0,
            "quality_floor_met": True,
            "predicted_video_bytes": 1_000_000,
            "predicted_whole_episode_bytes": 5_000_000,
            "violates_source_cap": False,
        }
        bracket = {
            "crf": 30.0,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 86.5,
            "quality_floor_met": True,
            "predicted_video_bytes": 2_333_333,
            "predicted_whole_episode_bytes": 6_333_333,
            "violates_source_cap": True,
        }
        quality = QualitySearchResult(
            crf=34.0,
            metric="VMAF",
            target=85.0,
            score=86.0,
            stdout="target-size-search",
            target_size_trace={
                "target": {
                    "total_target_bytes": 5_000_000,
                    "non_video_bytes": 4_000_000,
                    "sample_projection_tolerance_percent": 10.0,
                },
                "source_cap": {"video_cap_bytes": 1_500_000},
                "quality_floor": {"metric": "VMAF", "target": 85.0, "minimum": 80.0},
                "candidates": [selected, bracket],
                "selected_candidate": selected,
            },
        )
        verification = verify_final_output_size(
            self._ledger(target_bytes=5_000_000, source_size_bytes=20_000_000),
            4_600_000,
            retry_count=0,
        )
        measured_crfs: list[float] = []

        def measure_candidate(crf: float) -> SampleEncodeResult:
            measured_crfs.append(crf)
            return SampleEncodeResult("VMAF", 86.2, 20.0, 30.0, 1_700_000, "measured")

        retry_quality = retry_quality_result_for_final_miss(
            quality,
            verification,
            measure_candidate=measure_candidate,
        )

        self.assertIsNotNone(retry_quality)
        assert retry_quality is not None
        self.assertEqual(measured_crfs, [32.0])
        self.assertEqual(retry_quality.crf, 32.0)
        self.assertEqual(
            retry_quality.target_size_trace["final_retry_calibration"][
                "calibrated_predicted_whole_episode_bytes"
            ],
            5_020_000,
        )
        self.assertTrue(retry_quality.target_size_trace["selected_candidate"]["sample_projection_violates_source_cap"])
        self.assertFalse(retry_quality.target_size_trace["selected_candidate"]["violates_source_cap"])
        self.assertTrue(retry_quality.target_size_trace["selected_candidate"]["within_calibrated_final_band"])

    def test_final_output_retry_requires_a_bracket_with_an_intermediate_integer_crf(self) -> None:
        selected = {
            "crf": 34.0,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 86.0,
            "quality_floor_met": True,
            "predicted_video_bytes": 1_000_000,
            "predicted_whole_episode_bytes": 5_000_000,
            "violates_source_cap": False,
        }
        verification = verify_final_output_size(
            self._ledger(target_bytes=5_000_000, source_size_bytes=20_000_000),
            5_400_000,
            retry_count=0,
        )
        cases = (
            (31.0, 900_000, "no corrected target bracket"),
            (35.0, 500_000, "no intermediate integer crf"),
        )
        for bracket_crf, bracket_video_bytes, label in cases:
            with self.subTest(label=label):
                bracket = {
                    "crf": bracket_crf,
                    "metric": "VMAF",
                    "metric_target": 85.0,
                    "metric_score": 84.0,
                    "quality_floor_met": True,
                    "predicted_video_bytes": bracket_video_bytes,
                    "predicted_whole_episode_bytes": bracket_video_bytes + 4_000_000,
                    "violates_source_cap": False,
                }
                quality = QualitySearchResult(
                    crf=34.0,
                    metric="VMAF",
                    target=85.0,
                    score=86.0,
                    stdout="target-size-search",
                    target_size_trace={
                        "target": {
                            "total_target_bytes": 5_000_000,
                            "non_video_bytes": 4_000_000,
                            "sample_projection_tolerance_percent": 10.0,
                        },
                        "source_cap": {"video_cap_bytes": 10_000_000},
                        "quality_floor": {"metric": "VMAF", "target": 85.0, "minimum": 80.0},
                        "candidates": [selected, bracket],
                        "selected_candidate": selected,
                    },
                )

                retry_quality = retry_quality_result_for_final_miss(
                    quality,
                    verification,
                    measure_candidate=lambda _crf: self.fail("Unsafe retries must not be measured"),
                )

                self.assertIsNone(retry_quality)

    def test_final_output_retry_interpolation_respects_calibrated_source_cap(self) -> None:
        selected = {
            "crf": 30.0,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 86.0,
            "quality_floor_met": True,
            "predicted_video_bytes": 800_000,
            "predicted_whole_episode_bytes": 900_000,
            "violates_source_cap": False,
        }
        bracket = {
            "crf": 36.0,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 82.0,
            "quality_floor_met": True,
            "predicted_video_bytes": 500_000,
            "predicted_whole_episode_bytes": 600_000,
            "violates_source_cap": False,
        }
        quality = QualitySearchResult(
            crf=30.0,
            metric="VMAF",
            target=85.0,
            score=86.0,
            stdout="target-size-search",
            target_size_trace={
                "target": {
                    "total_target_bytes": 1_000_000,
                    "non_video_bytes": 100_000,
                    "sample_projection_tolerance_percent": 10.0,
                },
                "source_cap": {"video_cap_bytes": 870_000},
                "quality_floor": {"metric": "VMAF", "target": 85.0, "minimum": 80.0},
                "candidates": [selected, bracket],
                "selected_candidate": selected,
            },
        )
        verification = verify_final_output_size(
            self._ledger(target_bytes=1_000_000, source_size_bytes=2_000_000),
            1_100_000,
            retry_count=0,
        )
        measured_crfs: list[float] = []

        def measure_candidate(crf: float) -> SampleEncodeResult:
            measured_crfs.append(crf)
            return SampleEncodeResult("VMAF", 84.0, 20.0, 30.0, 683_000, "measured")

        retry_quality = retry_quality_result_for_final_miss(
            quality,
            verification,
            measure_candidate=measure_candidate,
        )

        self.assertIsNotNone(retry_quality)
        assert retry_quality is not None
        self.assertEqual(measured_crfs, [32.0])
        self.assertEqual(retry_quality.crf, 32.0)
        self.assertLessEqual(
            retry_quality.target_size_trace["selected_candidate"]["calibrated_predicted_video_bytes"],
            870_000,
        )

    def test_final_output_retry_rejects_non_finite_quality_score(self) -> None:
        selected = {
            "crf": 30.0,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 86.0,
            "quality_floor_met": True,
            "predicted_video_bytes": 1_000_000,
            "predicted_whole_episode_bytes": 5_000_000,
            "violates_source_cap": False,
        }
        bracket = {
            "crf": 34.0,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 82.0,
            "quality_floor_met": True,
            "predicted_video_bytes": 200_000,
            "predicted_whole_episode_bytes": 4_200_000,
            "violates_source_cap": False,
        }
        quality = QualitySearchResult(
            crf=30.0,
            metric="VMAF",
            target=85.0,
            score=86.0,
            stdout="target-size-search",
            target_size_trace={
                "target": {
                    "total_target_bytes": 5_000_000,
                    "non_video_bytes": 4_000_000,
                    "sample_projection_tolerance_percent": 10.0,
                },
                "source_cap": {"video_cap_bytes": 10_000_000},
                "quality_floor": {"metric": "VMAF", "target": 85.0, "minimum": 80.0},
                "candidates": [selected, bracket],
                "selected_candidate": selected,
            },
        )
        verification = verify_final_output_size(
            self._ledger(target_bytes=5_000_000, source_size_bytes=20_000_000),
            5_400_000,
            retry_count=0,
        )

        retry_quality = retry_quality_result_for_final_miss(
            quality,
            verification,
            measure_candidate=lambda _crf: SampleEncodeResult(
                "VMAF", float("nan"), 20.0, 30.0, 800_000, "invalid score"
            ),
        )

        self.assertIsNone(retry_quality)
        self.assertEqual(
            quality.target_size_trace["selection_reason"],
            "final_retry_measurement_invalid_quality_score",
        )

    def test_search_rejects_mutated_transform_plan_identity(self) -> None:
        transform_plan = self._transform_plan()
        transform_plan["video_filter"] = "scale=-2:720:flags=lanczos"

        with self.assertRaises(TargetSizeSearchError) as context:
            search_target_size(
                Path("/tmp/source.mkv"),
                self._policy(),
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
                stream_budget_ledger=self._ledger(target_bytes=300_000_000, source_size_bytes=1_000_000_000),
                transform_plan=transform_plan,
                process_controller=None,
                host=None,
                quality_temp_dir=None,
                run_sample_encode=lambda *_args, **_kwargs: self.fail("invalid plans must fail before sampling"),
            )

        self.assertEqual(context.exception.status, "needs_review")
        self.assertEqual(context.exception.trace["selection_reason"], "transform_plan_identity_invalid")

    def _ledger(
            self,
            *,
            target_bytes: int,
            source_size_bytes: int,
            max_encoded_percent: int = 80,
    ) -> StreamBudgetLedger:
        item = {
            "library_item_id": 7,
            "rel_path": "tv/show/episode.mkv",
            "source_fingerprint": "fingerprint-7",
            "source_size_bytes": source_size_bytes,
            "video_bitrate": 8_000_000,
            "duration_seconds": 3600.0,
            "output_container": "mkv",
            "resolved_policy": self._policy(max_encoded_percent=max_encoded_percent),
            "audio_summary": [],
            "subtitle_summary": [],
            "attachment_summary": [],
        }
        size_goal = SizeGoalIntent(
            mode="absolute",
            value_bytes=target_bytes,
            reference_runtime_seconds=None,
            sample_projection_tolerance_percent=10.0,
            final_output_tolerance_percent=5.0,
            source="test",
        ).resolve(3600.0)
        return resolve_stream_budget_ledger(item, resolved_size_goal=size_goal, prefer_persisted=False)

    @staticmethod
    def _policy(*, max_encoded_percent: int = 80) -> dict[str, object]:
        return {
            "video": {
                "encoder": "libsvtav1",
                "pixel_format": "yuv420p10le",
                "preset": 4,
                "quality_metric": "vmaf",
                "target_vmaf": 85.0,
                "min_target_vmaf": 80.0,
                "target_xpsnr": 39.0,
                "min_target_xpsnr": 35.0,
                "sample_every": "8m",
                "sample_duration": "20s",
                "min_crf": 18,
                "max_crf": 38,
                "max_encoded_percent": max_encoded_percent,
                "default_grain": 0,
                "grain_denoise": 0,
            },
            "audio": {},
            "subtitle": {},
        }

    @staticmethod
    def _transform_plan(*, video_filter: str | None = None) -> dict[str, Any]:
        return build_target_size_transform_plan(
            cadence_evidence_id="ev1_cadence",
            cadence_class="progressive",
            cadence_transform="none",
            video_filter=video_filter,
        )


if __name__ == "__main__":
    unittest.main()
