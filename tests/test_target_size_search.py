import unittest
from pathlib import Path
from typing import Any

from mediaforce.encoding.quality import SampleEncodeResult
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

    def test_source_cap_below_target_band_is_infeasible_without_sampling(self) -> None:
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
            )

        self.assertEqual(calls, 0)
        self.assertEqual(context.exception.status, "infeasible")
        self.assertEqual(context.exception.trace["selection_reason"], "target_lower_bound_exceeds_source_relative_cap")

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
        sizes = {31: 100_000_000, 18: 500_000_000, 24: 360_000_000, 28: 370_000_000, 30: 380_000_000, 29: 390_000_000}

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

    def test_final_output_verification_allows_only_bounded_retry_from_measured_candidates(self) -> None:
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
        retry_quality = retry_quality_result_for_final_miss(quality, verification)

        self.assertIsNotNone(retry_quality)
        self.assertEqual(retry_quality.crf, 28.0)
        exhausted = verify_final_output_size(ledger, 330_000_000, retry_count=1)
        self.assertFalse(exhausted.retry_allowed)
        self.assertIsNone(retry_quality_result_for_final_miss(quality, exhausted))

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
