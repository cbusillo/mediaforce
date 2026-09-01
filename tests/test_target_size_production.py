import json
import sqlite3
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, cast
from unittest.mock import patch

from sqlalchemy import select

from mediaforce import execution
from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import item_events, library_items, quality_search_observations, staged_artifacts
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.models import ProbeSummary
from mediaforce.core.process_control import ProcessCancelledError, ScheduleWindowClosedError
from mediaforce.encoding.quality import (
    QualitySearchError,
    QualitySearchResult,
    QualityTempSetupError,
    SampleEncodeResult,
)
from mediaforce.tuning.quality_memory import quality_search_context_from_command, rounded_target_video_bitrate
from mediaforce.tuning.quality_shadow import QualityShadowMetrics, QualityShadowRecommendation
from mediaforce.tuning.quality_warm_start import QualityWarmStartBlockReason, QualityWarmStartPlan
from mediaforce.tuning.target_size_search import FinalSizeMissError, TargetSizeSearchError


class TargetSizeProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self._config()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_encode_persists_final_size_verification_inside_approved_band(self) -> None:
        source_path = self._source_file("episode-target-ok.mkv")
        staging_path = self._staging_path("episode-target-ok.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=28.0),
            )

            build_calls, measure_calls = self._encode_with_output_sizes(connection, item, quality, [5_100_000])

            artifact = self._staged_artifact(connection, item_id, staged_artifacts.c.validation_json)
            assert artifact is not None
            validation = json.loads(cast(str, artifact["validation_json"]))
            final_output = validation["target_size_trace"]["final_output"]
            self.assertTrue(validation["passed"])
            self.assertEqual(final_output["status"], "inside_target_band")
            self.assertEqual(final_output["actual_output_bytes"], 5_100_000)
            self.assertEqual(validation["target_size_trace"]["selected_candidate"]["predicted_whole_episode_bytes"], 5_000_000)
            self.assertEqual(len(build_calls), 1)
            self.assertEqual(measure_calls, [])
            observation = connection.execute(select(quality_search_observations)).mappings().one()
            self.assertEqual(observation["outcome_kind"], "selected")
            self.assertEqual(observation["learning_eligible"], 1)
            self.assertEqual(observation["actual_output_bytes"], 5_100_000)
            self.assertEqual(observation["candidate_count"], 2)
            self.assertIsNotNone(observation["policy_hash"])
            shadow = json.loads(cast(str, observation["shadow_json"]))
            self.assertEqual(shadow["fallback_reason"], "no_history")
            self.assertFalse(shadow["production_search_changed"])
            self.assertEqual(shadow["comparison"]["candidate_count"], 2)
            self.assertEqual(shadow["comparison"]["size_error_percent"], 2.0)

    def test_quality_only_encode_persists_structured_observation(self) -> None:
        source_path = self._source_file("episode-quality-only.mkv")
        staging_path = self._staging_path("episode-quality-only.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            quality = QualitySearchResult(
                crf=30.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="crf 30 VMAF 86 predicted video stream size 5 MiB (25%)",
            )

            self._encode_with_output_sizes(connection, item, quality, [5_100_000])

            observation = connection.execute(select(quality_search_observations)).mappings().one()
            trace = json.loads(cast(str, observation["candidate_trace_json"]))
            self.assertEqual(observation["search_objective"], "quality")
            self.assertEqual(observation["candidate_count"], 1)
            self.assertEqual(trace["candidates"][0]["predicted_encode_size_bytes"], 5 * 1024 ** 2)
            self.assertNotIn("line", trace["candidates"][0])

    def test_passive_plan_does_not_execute_a_warm_start(self) -> None:
        source_path = self._source_file("episode-warm-selected.mkv")
        staging_path = self._staging_path("episode-warm-selected.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=28.0),
            )
            search_calls: list[dict[str, Any]] = []
            policy_before = json.loads(json.dumps(item["resolved_policy"]))

            self._encode_with_output_sizes(
                connection,
                item,
                quality,
                [5_100_000],
                warm_start_plan=self._warm_start_plan(first_crf=28.0),
                search_call_kwargs=search_calls,
            )

            observation = connection.execute(select(quality_search_observations)).mappings().one()
            shadow = json.loads(cast(str, observation["shadow_json"]))
            outcome = json.loads(cast(str, observation["outcome_json"]))
            self.assertEqual(observation["learning_eligible"], 1)
            self.assertIsNone(observation["exclusion_reason"])
            self.assertEqual(outcome["exploration_kind"], "full_search")
            self.assertFalse(shadow["production_search_changed"])
            self.assertEqual(shadow["warm_start"]["execution_mode"], "passive")
            self.assertNotIn("warm_start", search_calls[0])
            self.assertEqual(item["resolved_policy"], policy_before)

    def test_blocked_plan_keeps_passive_search_authoritative(self) -> None:
        source_path = self._source_file("episode-warm-blocked.mkv")
        staging_path = self._staging_path("episode-warm-blocked.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=28.0),
            )
            search_calls: list[dict[str, Any]] = []

            self._encode_with_output_sizes(
                connection,
                item,
                quality,
                [5_100_000],
                warm_start_plan=self._warm_start_plan(
                    first_crf=28.0,
                    block_reason="shadow_thresholds_not_met",
                ),
                search_call_kwargs=search_calls,
            )

            self.assertNotIn("warm_start", search_calls[0])
            observation = connection.execute(select(quality_search_observations)).mappings().one()
            shadow = json.loads(cast(str, observation["shadow_json"]))
            self.assertFalse(shadow["production_search_changed"])
            self.assertEqual(shadow["fallback_reason"], "no_history")
            self.assertFalse(shadow["warm_start"]["eligible"])
            self.assertEqual(shadow["warm_start"]["block_reason"], "shadow_thresholds_not_met")

    def test_passive_plan_runs_full_search_and_remains_learning_evidence(self) -> None:
        source_path = self._source_file("episode-warm-holdout.mkv")
        staging_path = self._staging_path("episode-warm-holdout.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=28.0),
            )
            search_calls: list[dict[str, Any]] = []

            self._encode_with_output_sizes(
                connection,
                item,
                quality,
                [5_100_000],
                warm_start_plan=self._warm_start_plan(
                    first_crf=28.0,
                    experiment_arm="passive",
                ),
                search_call_kwargs=search_calls,
            )

            self.assertNotIn("warm_start", search_calls[0])
            observation = connection.execute(select(quality_search_observations)).mappings().one()
            shadow = json.loads(cast(str, observation["shadow_json"]))
            outcome = json.loads(cast(str, observation["outcome_json"]))
            timing = json.loads(cast(str, observation["timing_json"]))
            self.assertEqual(observation["learning_eligible"], 1)
            self.assertEqual(outcome["quality_memory_arm"], "passive")
            self.assertEqual(outcome["quality_memory_experiment_version"], "qwa1")
            self.assertEqual(outcome["exploration_kind"], "full_search")
            self.assertTrue(shadow["warm_start"]["eligible"])
            self.assertEqual(shadow["warm_start"]["experiment_arm"], "passive")
            self.assertGreater(timing["workflow_duration_seconds"], 0)

    def test_passive_plan_does_not_run_a_second_warm_start_search(self) -> None:
        source_path = self._source_file("episode-warm-final-miss.mkv")
        staging_path = self._staging_path("episode-warm-final-miss.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            baseline_quality = QualitySearchResult(
                crf=30.0,
                metric="VMAF",
                target=85.0,
                score=85.5,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=30.0),
            )
            search_calls: list[dict[str, Any]] = []

            build_calls, measure_calls = self._encode_with_output_sizes(
                connection,
                item,
                baseline_quality,
                [5_100_000],
                warm_start_plan=self._warm_start_plan(first_crf=28.0),
                search_results=[baseline_quality],
                search_call_kwargs=search_calls,
            )

            observation = connection.execute(select(quality_search_observations)).mappings().one()
            shadow = json.loads(cast(str, observation["shadow_json"]))
            outcome = json.loads(cast(str, observation["outcome_json"]))
            self.assertEqual(len(search_calls), 1)
            self.assertNotIn("warm_start", search_calls[0])
            self.assertEqual(len(build_calls), 1)
            self.assertEqual(measure_calls, [])
            self.assertEqual(observation["selected_crf"], 30.0)
            self.assertEqual(observation["learning_eligible"], 1)
            self.assertEqual(outcome["exploration_kind"], "full_search")
            self.assertFalse(shadow["production_search_changed"])
            self.assertFalse(shadow["comparison"]["final_size_miss"])
            self.assertEqual(shadow["comparison"]["candidate_count"], 2)
            event_types = connection.execute(select(item_events.c.event_type)).scalars().all()
            self.assertNotIn("encoding_quality_warm_start_fallback", event_types)

    def test_same_crf_fallback_still_runs_the_unchanged_full_encode(self) -> None:
        source_path = self._source_file("episode-warm-same-crf.mkv")
        staging_path = self._staging_path("episode-warm-same-crf.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            warm_quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._warm_trace(item, selected_crf=28.0, status="accepted"),
            )
            baseline_quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=28.0),
            )
            search_calls: list[dict[str, Any]] = []
            encode_count = 0

            def count_encode() -> None:
                nonlocal encode_count
                encode_count += 1

            with self.assertRaises(FinalSizeMissError):
                self._encode_with_output_sizes(
                    connection,
                    item,
                    warm_quality,
                    [5_500_000, 5_500_000],
                    during_encode=count_encode,
                    warm_start_plan=self._warm_start_plan(first_crf=28.0),
                    search_results=[warm_quality, baseline_quality],
                    search_call_kwargs=search_calls,
                )

            self.assertEqual(len(search_calls), 2)
            self.assertEqual(encode_count, 2)
            event_types = connection.execute(select(item_events.c.event_type)).scalars().all()
            self.assertIn("encoding_needs_review", event_types)
            self.assertNotIn("encoding_failed", event_types)

    def test_failed_fresh_fallback_remains_a_needs_review_outcome(self) -> None:
        source_path = self._source_file("episode-warm-fallback-failed.mkv")
        staging_path = self._staging_path("episode-warm-fallback-failed.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            warm_quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._warm_trace(item, selected_crf=28.0, status="accepted"),
            )
            baseline_error = TargetSizeSearchError(
                "baseline search could not select a candidate",
                status="needs_review",
                trace={"candidate_count": 2, "candidates": [{"crf": 30.0}, {"crf": 32.0}]},
            )

            with self.assertRaisesRegex(FinalSizeMissError, "fresh baseline search also failed"):
                self._encode_with_output_sizes(
                    connection,
                    item,
                    warm_quality,
                    [5_500_000],
                    warm_start_plan=self._warm_start_plan(first_crf=28.0),
                    search_results=[warm_quality, baseline_error],
                )

            event_types = connection.execute(select(item_events.c.event_type)).scalars().all()
            self.assertIn("encoding_needs_review", event_types)
            self.assertNotIn("encoding_failed", event_types)
            observation = connection.execute(select(quality_search_observations)).mappings().one()
            shadow = json.loads(cast(str, observation["shadow_json"]))
            self.assertEqual(observation["outcome_kind"], "final_size_failure")
            self.assertEqual(observation["learning_eligible"], 0)
            self.assertEqual(observation["exclusion_reason"], "final_size_miss")
            self.assertEqual(shadow["warm_start"]["execution"]["fallback_reason"], "baseline_search_failed")
            self.assertEqual(shadow["warm_start"]["execution"]["baseline_candidate_count"], 2)
            self.assertEqual(shadow["comparison"]["candidate_count"], 3)

    def test_shadow_evaluation_failure_never_masks_a_completed_encode(self) -> None:
        source_path = self._source_file("episode-shadow-failure.mkv")
        staging_path = self._staging_path("episode-shadow-failure.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            quality = QualitySearchResult(
                crf=30.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="crf 30 VMAF 86 predicted video stream size 5 MiB (25%)",
            )

            with self.assertLogs("mediaforce.encoding.manifest", level="WARNING"), patch(
                "mediaforce.encoding.manifest.plan_quality_warm_start",
                side_effect=RuntimeError("warm start unavailable"),
            ), patch(
                "mediaforce.encoding.manifest.recommend_quality_search",
                side_effect=RuntimeError("shadow unavailable"),
            ):
                self._encode_with_output_sizes(connection, item, quality, [5_100_000])

            observation = connection.execute(select(quality_search_observations)).mappings().one()
            shadow = json.loads(cast(str, observation["shadow_json"]))
            self.assertEqual(shadow["fallback_reason"], "shadow_evaluation_error")
            self.assertFalse(shadow["production_search_changed"])

    def test_relaxed_quality_target_is_recorded_without_a_savings_projection(self) -> None:
        source_path = self._source_file("episode-relaxed-target.mkv")
        staging_path = self._staging_path("episode-relaxed-target.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            quality = QualitySearchResult(
                crf=30.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="",
                quality_search_trace={
                    "initial_target": 86.0,
                    "attempts": [
                        {
                            "metric_target": 86.0,
                            "status": "no_selection",
                            "candidate_count": 2,
                            "candidates": [
                                {"crf": 26.0, "metric_score": 87.0},
                                {"crf": 28.0, "metric_score": 86.0},
                            ],
                        },
                        {
                            "metric_target": 85.0,
                            "status": "selected",
                            "candidate_count": 2,
                            "candidates": [
                                {"crf": 29.0, "metric_score": 85.5},
                                {"crf": 30.0, "metric_score": 86.0},
                            ],
                        },
                    ],
                },
            )

            self._encode_with_output_sizes(connection, item, quality, [5_100_000])

            observation = connection.execute(select(quality_search_observations)).mappings().one()
            shadow = json.loads(cast(str, observation["shadow_json"]))
            self.assertEqual(shadow["fallback_reason"], "search_target_changed")
            self.assertIsNone(shadow["recommendation"])
            self.assertEqual(shadow["comparison"]["candidate_count"], 4)

    def test_runtime_signature_uses_final_probe_and_command_context(self) -> None:
        source_path = self._source_file("episode-context-parity.mkv")
        staging_path = self._staging_path("episode-context-parity.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=28.0),
            )
            command = [
                "ffmpeg",
                "-i",
                str(source_path),
                "-c:v",
                "libsvtav1",
                "-pix_fmt",
                "yuv420p10le",
                "-preset",
                "4",
                "-svtav1-params",
                "tune=0:film-grain=0:film-grain-denoise=0",
                "-vf",
                "scale=-2:720",
                str(staging_path),
            ]

            self._encode_with_output_sizes(
                connection,
                item,
                quality,
                [5_100_000],
                encode_command=command,
                staged_width=1280,
                staged_height=720,
            )

            observation = connection.execute(select(quality_search_observations)).mappings().one()
            target_video_bitrate = rounded_target_video_bitrate(
                target_video_bytes=1_000_000,
                source_duration_seconds=60.0,
            )
            expected = quality_search_context_from_command(
                command,
                metric="VMAF",
                target=85.0,
                minimum_quality_score=80.0,
                search_objective="target_size",
                size_target_bytes=5_000_000,
                target_video_bitrate=target_video_bitrate,
                source_codec="h264",
                output_width=1280,
                output_height=720,
            )
            self.assertEqual(observation["search_signature_id"], expected.signature_id)
            self.assertEqual(observation["learning_eligible"], 1)
            context = json.loads(cast(str, observation["context_json"]))
            self.assertEqual(context["search"]["target_video_bitrate"], 133_000)
            self.assertEqual(context["search"]["output_width"], 1280)
            self.assertEqual(context["search"]["video_filter"], "scale=-2:720")

    def test_success_with_incomplete_context_persists_diagnostic_observation(self) -> None:
        source_path = self._source_file("episode-incomplete-context.mkv")
        staging_path = self._staging_path("episode-incomplete-context.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            quality = QualitySearchResult(30.0, "VMAF", 85.0, 86.0, "")

            self._encode_with_output_sizes(
                connection,
                item,
                quality,
                [5_100_000],
                encode_command=["ffmpeg", "-i", str(source_path), str(staging_path)],
            )

            observation = connection.execute(select(quality_search_observations)).mappings().one()
            self.assertEqual(observation["outcome_kind"], "selected")
            self.assertEqual(observation["learning_eligible"], 0)
            self.assertEqual(observation["exclusion_reason"], "search_context_incomplete")
            self.assertIsNone(observation["search_signature_id"])
            self.assertIsNone(observation["shadow_json"])

    def test_parseable_quality_failure_persists_diagnostic_observation(self) -> None:
        source_path = self._source_file("episode-quality-failure.mkv")
        staging_path = self._staging_path("episode-quality-failure.mkv")
        error = QualitySearchError(
            "crf 38 VMAF 84 predicted video stream size 17 MiB (85%)\n"
            "Error: Failed to find a suitable crf"
        )
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            with patch("mediaforce.execution.resolve_item_source_path", return_value=source_path), patch(
                "mediaforce.execution.resolve_item_staging_path", return_value=staging_path
            ), patch("mediaforce.execution._search_quality", side_effect=error):
                with self.assertRaises(QualitySearchError):
                    execution.encode_one_item(
                        connection,
                        self.config,
                        self.root / "runs" / "manifest.json",
                        {"run_id": "quality-failure-run", "items": [item]},
                        0,
                        item,
                        overwrite=False,
                    )

            observation = connection.execute(select(quality_search_observations)).mappings().one()
            self.assertEqual(observation["outcome_kind"], "deterministic_search_failure")
            self.assertEqual(observation["learning_eligible"], 0)
            self.assertEqual(observation["exclusion_reason"], "quality_search_exhausted")
            self.assertEqual(observation["candidate_count"], 1)

    def test_operational_search_failures_do_not_persist_observations(self) -> None:
        failures = (
            ScheduleWindowClosedError("schedule closed"),
            ProcessCancelledError("cancelled"),
            QualityTempSetupError("storage unavailable"),
            BlockingIOError("resource busy"),
            TimeoutError("ssh transport timeout"),
        )
        for index, error in enumerate(failures, start=1):
            with self.subTest(error=type(error).__name__):
                source_path = self._source_file(f"episode-operational-{index}.mkv")
                staging_path = self._staging_path(f"episode-operational-{index}.mkv")
                with open_db(self.config.paths.db_path) as connection:
                    item_id = self._insert_item(connection, source_path)
                    item = self._manifest_item(item_id, source_path, staging_path)
                    with patch("mediaforce.execution.resolve_item_source_path", return_value=source_path), patch(
                        "mediaforce.execution.resolve_item_staging_path", return_value=staging_path
                    ), patch("mediaforce.execution._search_quality", side_effect=error):
                        with self.assertRaises(type(error)):
                            execution.encode_one_item(
                                connection,
                                self.config,
                                self.root / "runs" / "manifest.json",
                                {"run_id": f"operational-{index}", "items": [item]},
                                0,
                                item,
                                overwrite=False,
                            )
                    count = connection.execute(
                        select(quality_search_observations.c.observation_id)
                    ).all()
                    self.assertEqual(count, [])

    def test_encode_uses_one_logged_retry_from_measured_candidate_for_final_miss(self) -> None:
        source_path = self._source_file("episode-target-retry.mkv")
        staging_path = self._staging_path("episode-target-retry.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=28.0, retry_crf=31.0),
            )

            build_calls, measure_calls = self._encode_with_output_sizes(
                connection,
                item,
                quality,
                [5_400_000, 5_100_000],
            )

            artifact = self._staged_artifact(connection, item_id, staged_artifacts.c.validation_json)
            assert artifact is not None
            validation = json.loads(cast(str, artifact["validation_json"]))
            final_output = validation["target_size_trace"]["final_output"]
            events = self._events(connection, item_id)
            self.assertTrue(validation["passed"])
            self.assertEqual(final_output["retry_count"], 1)
            self.assertEqual(final_output["actual_output_bytes"], 5_100_000)
            self.assertIn("encoding_target_size_retry", [event["event_type"] for event in events])
            self.assertEqual([call.kwargs["quality"].crf for call in build_calls], [28.0, 31.0])
            self.assertEqual(measure_calls, [])

    def test_encode_measures_interpolated_retry_before_second_full_encode(self) -> None:
        source_path = self._source_file("episode-target-measured-retry.mkv")
        staging_path = self._staging_path("episode-target-measured-retry.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=34.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(
                    item,
                    selected_crf=34.0,
                    retry_crf=38.0,
                    retry_predicted_total=4_200_000,
                ),
            )
            retry_sample = SampleEncodeResult(
                metric="VMAF",
                score=84.5,
                predicted_encode_percent=20.0,
                predicted_encode_seconds=30.0,
                predicted_encode_size_bytes=800_000,
                stdout="measured retry",
            )

            build_calls, measure_calls = self._encode_with_output_sizes(
                connection,
                item,
                quality,
                [5_400_000, 5_100_000],
                retry_sample=retry_sample,
            )

            artifact = self._staged_artifact(
                connection,
                item_id,
                staged_artifacts.c.chosen_crf,
                staged_artifacts.c.quality_score,
                staged_artifacts.c.validation_json,
            )
            assert artifact is not None
            validation = json.loads(cast(str, artifact["validation_json"]))
            attempts = validation["target_size_trace"]["final_output_attempts"]
            self.assertEqual([call.kwargs["quality"].crf for call in build_calls], [34.0, 35.0])
            self.assertEqual([call.kwargs["crf"] for call in measure_calls], [35.0])
            self.assertEqual(artifact["chosen_crf"], 35.0)
            self.assertEqual(artifact["quality_score"], 84.5)
            self.assertEqual([attempt["status"] for attempt in attempts], ["over_target", "inside_target_band"])
            self.assertEqual(
                validation["target_size_trace"]["selected_candidate"]["role"],
                "final_retry_measurement",
            )

    def test_encode_performs_at_most_one_replacement_full_encode(self) -> None:
        source_path = self._source_file("episode-target-directional-probe.mkv")
        staging_path = self._staging_path("episode-target-directional-probe.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            trace = self._trace(item, selected_crf=34.0)
            trace["crf_bounds"] = {"min_crf": 18, "max_crf": 38}
            quality = QualitySearchResult(
                crf=34.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=trace,
            )
            retry_sample = SampleEncodeResult(
                metric="VMAF",
                score=84.5,
                predicted_encode_percent=20.0,
                predicted_encode_seconds=30.0,
                predicted_encode_size_bytes=800_000,
                stdout="bounded directional probe",
            )

            build_calls, measure_calls = self._encode_with_output_sizes(
                connection,
                item,
                quality,
                [5_400_000, 5_100_000],
                retry_sample=retry_sample,
            )

            artifact = self._staged_artifact(
                connection,
                item_id,
                staged_artifacts.c.chosen_crf,
                staged_artifacts.c.validation_json,
            )
            assert artifact is not None
            validation = json.loads(cast(str, artifact["validation_json"]))
            retry_trace = validation["target_size_trace"]
            self.assertEqual([call.kwargs["quality"].crf for call in build_calls], [34.0, 37.0])
            self.assertEqual([call.kwargs["crf"] for call in measure_calls], [37.0])
            self.assertEqual(artifact["chosen_crf"], 37.0)
            self.assertEqual(retry_trace["final_retry_calibration"]["strategy"], "bounded_directional_probe")
            self.assertEqual(retry_trace["final_retry_calibration"]["probe_from_crf"], 34.0)
            self.assertEqual(retry_trace["final_retry_calibration"]["measurement_count"], 1)
            self.assertEqual(
                retry_trace["final_retry_calibration"]["measurements"][0]["placement"],
                "directional_seed",
            )

    def test_encode_persists_three_measurement_straddle_retry(self) -> None:
        source_path = self._source_file("episode-target-straddle-retry.mkv")
        staging_path = self._staging_path("episode-target-straddle-retry.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            trace = self._trace(item, selected_crf=34.0)
            trace["crf_bounds"] = {"min_crf": 18, "max_crf": 38}
            quality = QualitySearchResult(
                crf=34.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=trace,
            )
            retry_samples = [
                SampleEncodeResult("VMAF", 85.5, 20.0, 30.0, 990_000, "directional seed"),
                SampleEncodeResult("VMAF", 84.5, 20.0, 30.0, 500_000, "measured secant"),
                SampleEncodeResult("VMAF", 85.0, 20.0, 30.0, 700_000, "bracket interpolation"),
            ]

            build_calls, measure_calls = self._encode_with_output_sizes(
                connection,
                item,
                quality,
                [5_300_000, 5_100_000],
                retry_samples=retry_samples,
            )

            artifact = self._staged_artifact(
                connection,
                item_id,
                staged_artifacts.c.chosen_crf,
                staged_artifacts.c.validation_json,
            )
            assert artifact is not None
            validation = json.loads(cast(str, artifact["validation_json"]))
            retry_trace = validation["target_size_trace"]
            measurements = retry_trace["final_retry_calibration"]["measurements"]
            self.assertEqual([call.kwargs["quality"].crf for call in build_calls], [34.0, 37.0])
            self.assertEqual([call.kwargs["crf"] for call in measure_calls], [36.0, 38.0, 37.0])
            self.assertEqual(artifact["chosen_crf"], 37.0)
            self.assertEqual(retry_trace["final_retry_calibration"]["measurement_count"], 3)
            self.assertEqual(
                [measurement["placement"] for measurement in measurements],
                ["directional_seed", "measured_secant", "bracket_interpolation"],
            )
            self.assertEqual(
                [attempt["status"] for attempt in retry_trace["final_output_attempts"]],
                ["over_target", "inside_target_band"],
            )

    def test_encode_surfaces_needs_review_when_final_miss_has_no_measured_retry(self) -> None:
        source_path = self._source_file("episode-target-review.mkv")
        staging_path = self._staging_path("episode-target-review.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=28.0),
            )

            with self.assertRaisesRegex(RuntimeError, "Final output size missed"):
                self._encode_with_output_sizes(connection, item, quality, [5_400_000])

            events = self._events(connection, item_id)
            event_types = [event["event_type"] for event in events]
            self.assertEqual(event_types.count("encoding_needs_review"), 1)
            self.assertNotIn("encoding_failed", event_types)
            failed_details = json.loads(cast(str, events[-1]["details_json"]))
            self.assertEqual(failed_details["failure_kind"], "target_size_needs_review")
            self.assertEqual(failed_details["target_size_verification"]["status"], "over_target")
            failed_trace = failed_details["target_size_trace"]
            self.assertEqual(failed_trace["status"], "needs_review")
            self.assertEqual(
                failed_trace["selection_reason"],
                "final_retry_skipped_no_eligible_directional_candidate",
            )
            self.assertEqual(failed_trace["final_retry_skip"]["status"], "skipped")
            self.assertEqual(
                failed_trace["final_retry_skip"]["reason"],
                "final_retry_skipped_no_eligible_directional_candidate",
            )
            self.assertNotIn("encoding_target_size_retry", event_types)
            observation = connection.execute(select(quality_search_observations)).mappings().one()
            outcome = json.loads(cast(str, observation["outcome_json"]))
            candidate_trace = json.loads(cast(str, observation["candidate_trace_json"]))
            self.assertEqual(observation["outcome_kind"], "final_size_failure")
            self.assertEqual(observation["learning_eligible"], 0)
            self.assertEqual(observation["exclusion_reason"], "final_size_miss")
            self.assertIsNotNone(observation["search_signature_id"])
            self.assertIsNone(observation["shadow_json"])
            self.assertTrue(outcome["search_selected"])
            self.assertFalse(outcome["terminal_success"])
            self.assertEqual(outcome["quality_memory_arm"], "ineligible")
            self.assertEqual(candidate_trace["status"], "needs_review")
            self.assertEqual(
                candidate_trace["selection_reason"],
                "final_retry_skipped_no_eligible_directional_candidate",
            )
            timing = json.loads(cast(str, observation["timing_json"]))
            self.assertGreater(timing["workflow_duration_seconds"], 0)
            self.assertFalse(staging_path.exists())

    def test_encode_persists_rejected_retry_measurement_reason(self) -> None:
        source_path = self._source_file("episode-target-retry-rejected.mkv")
        staging_path = self._staging_path("episode-target-retry-rejected.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=34.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(
                    item,
                    selected_crf=34.0,
                    retry_crf=38.0,
                    retry_predicted_total=4_200_000,
                ),
            )
            rejected_sample = SampleEncodeResult(
                metric="VMAF",
                score=79.5,
                predicted_encode_percent=20.0,
                predicted_encode_seconds=30.0,
                predicted_encode_size_bytes=800_000,
                stdout="below floor",
            )

            with self.assertRaisesRegex(RuntimeError, "Final output size missed"):
                self._encode_with_output_sizes(
                    connection,
                    item,
                    quality,
                    [5_400_000],
                    retry_sample=rejected_sample,
                )

            events = self._events(connection, item_id)
            failed_details = json.loads(cast(str, events[-1]["details_json"]))
            rejection_trace = failed_details["target_size_trace"]
            self.assertEqual(rejection_trace["selection_reason"], "final_retry_measurement_below_quality_floor")
            self.assertEqual(rejection_trace["final_retry_calibration"]["status"], "rejected")
            self.assertFalse(staging_path.exists())

    def test_encode_preserves_primary_error_when_failure_event_write_fails(self) -> None:
        source_path = self._source_file("episode-primary-failure.mkv")
        staging_path = self._staging_path("episode-primary-failure.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="quality-search",
            )

            def record_event(
                    _connection: DBClient,
                    _library_item_id: int,
                    event_type: str,
                    _details: dict[str, Any],
            ) -> None:
                if event_type == "encoding_failed":
                    raise sqlite3.OperationalError("database is locked")

            failed_process = subprocess.CompletedProcess(
                args=["ffmpeg"],
                returncode=1,
                stdout="",
                stderr="primary ffmpeg failure",
            )
            with patch("mediaforce.execution.resolve_item_source_path", return_value=source_path), patch(
                "mediaforce.execution.resolve_item_staging_path", return_value=staging_path
            ), patch("mediaforce.execution._search_quality", return_value=quality), patch(
                "mediaforce.execution._build_ffmpeg_command",
                return_value=["ffmpeg", "-i", str(source_path), str(staging_path)],
            ), patch("mediaforce.execution._run_encode_command", return_value=failed_process), patch(
                "mediaforce.execution._record_event",
                side_effect=record_event,
            ), patch(
                "mediaforce.encoding.manifest.safe_unlink",
                side_effect=OSError("cleanup failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "primary ffmpeg failure") as raised:
                    execution.encode_one_item(
                        connection,
                        self.config,
                        self.root / "runs" / "manifest.json",
                        {"run_id": "primary-failure-run", "items": [item]},
                        0,
                        item,
                        overwrite=False,
                    )
            self.assertIn(
                "Failed to persist encoding_failed event: database is locked",
                raised.exception.__notes__,
            )
            self.assertTrue(any("cleanup failed" in note for note in raised.exception.__notes__))
            self.assertEqual(connection.execute(select(1)).scalar_one(), 1)
            self.assertEqual(
                connection.execute(select(quality_search_observations.c.observation_id)).all(),
                [],
            )

    def test_warm_assigned_encode_failure_persists_acceptance_event_context(self) -> None:
        source_path = self._source_file("episode-warm-encode-failure.mkv")
        staging_path = self._staging_path("episode-warm-encode-failure.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="quality-search",
                target_size_trace=self._warm_trace(item, selected_crf=28.0, status="accepted"),
            )
            failed_process = subprocess.CompletedProcess(
                args=["ffmpeg"],
                returncode=1,
                stdout="",
                stderr="warm encode failure",
            )

            with patch(
                "mediaforce.encoding.manifest.plan_quality_warm_start",
                return_value=self._warm_start_plan(
                    first_crf=28.0,
                    experiment_arm="warm_start",
                ),
            ), patch(
                "mediaforce.execution.resolve_item_source_path",
                return_value=source_path,
            ), patch(
                "mediaforce.execution.resolve_item_staging_path",
                return_value=staging_path,
            ), patch(
                "mediaforce.execution._search_quality",
                return_value=quality,
            ), patch(
                "mediaforce.execution._build_ffmpeg_command",
                return_value=["ffmpeg", "-i", str(source_path), str(staging_path)],
            ), patch(
                "mediaforce.execution._run_encode_command",
                return_value=failed_process,
            ):
                with self.assertRaisesRegex(RuntimeError, "warm encode failure"):
                    execution.encode_one_item(
                        connection,
                        self.config,
                        self.root / "runs" / "manifest.json",
                        {"run_id": "warm-encode-failure-run", "items": [item]},
                        0,
                        item,
                        overwrite=False,
                    )

            failed_event = next(
                event for event in self._events(connection, item_id)
                if event["event_type"] == "encoding_failed"
            )
            details = json.loads(cast(str, failed_event["details_json"]))
            self.assertEqual(details["quality_memory_plan"]["experiment_version"], "qwa1")
            self.assertEqual(details["quality_memory_plan"]["experiment_arm"], "warm_start")
            self.assertEqual(details["quality_memory_policy_hash"], stable_json_hash(item["resolved_policy"]["video"]))

    def test_standalone_encode_allows_web_worker_write_during_media_work(self) -> None:
        source_path = self._source_file("episode-concurrent-writer.mkv")
        staging_path = self._staging_path("episode-concurrent-writer.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=28.0),
            )

            def web_worker_poll() -> None:
                with open_db(self.config.paths.db_path) as writer:
                    writer.exec_driver_sql("PRAGMA busy_timeout=100")
                    writer.execute(
                        item_events.insert().values(
                            library_item_id=item_id,
                            created_at="2026-07-14T00:00:00+00:00",
                            event_type="web_worker_poll",
                            details_json="{}",
                        )
                    )

            self._encode_with_output_sizes(
                connection,
                item,
                quality,
                [5_100_000],
                during_encode=web_worker_poll,
            )

            event_types = [event["event_type"] for event in self._events(connection, item_id)]
            self.assertIn("web_worker_poll", event_types)

    def test_encode_releases_caller_write_before_quality_search(self) -> None:
        source_path = self._source_file("episode-quality-writer.mkv")
        staging_path = self._staging_path("episode-quality-writer.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=28.0),
            )

            def web_worker_poll() -> None:
                with open_db(self.config.paths.db_path) as writer:
                    writer.exec_driver_sql("PRAGMA busy_timeout=100")
                    writer.execute(
                        item_events.insert().values(
                            library_item_id=item_id,
                            created_at="2026-07-14T00:00:00+00:00",
                            event_type="quality_search_web_poll",
                            details_json="{}",
                        )
                    )

            self._encode_with_output_sizes(
                connection,
                item,
                quality,
                [5_100_000],
                during_quality_search=web_worker_poll,
            )

            event_types = [event["event_type"] for event in self._events(connection, item_id)]
            self.assertIn("quality_search_web_poll", event_types)

    def test_retry_event_write_failure_does_not_abort_successful_encode(self) -> None:
        source_path = self._source_file("episode-retry-event-failure.mkv")
        staging_path = self._staging_path("episode-retry-event-failure.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=28.0, retry_crf=31.0),
            )
            record_event = execution._record_event

            def record_event_with_retry_failure(
                    event_connection: DBClient,
                    library_item_id: int,
                    event_type: str,
                    details: dict[str, Any],
            ) -> None:
                if event_type == "encoding_target_size_retry":
                    raise sqlite3.OperationalError("database is locked")
                record_event(event_connection, library_item_id, event_type, details)

            with patch("mediaforce.execution._record_event", side_effect=record_event_with_retry_failure):
                self._encode_with_output_sizes(connection, item, quality, [5_400_000, 5_100_000])

            artifact = self._staged_artifact(connection, item_id, staged_artifacts.c.validation_json)
            assert artifact is not None
            validation = json.loads(cast(str, artifact["validation_json"]))
            event_types = [event["event_type"] for event in self._events(connection, item_id)]
            self.assertTrue(staging_path.exists())
            self.assertIn("encoding_completed", event_types)
            self.assertNotIn("encoding_failed", event_types)
            self.assertEqual(
                validation["target_size_trace"]["event_persistence_errors"],
                ["Failed to persist encoding_target_size_retry event: database is locked"],
            )

    def test_completion_event_write_failure_preserves_successful_encode(self) -> None:
        source_path = self._source_file("episode-completion-event-failure.mkv")
        staging_path = self._staging_path("episode-completion-event-failure.mkv")
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, source_path)
            item = self._manifest_item(item_id, source_path, staging_path)
            self._attach_stream_budget(item)
            quality = QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="target-size-search",
                target_size_trace=self._trace(item, selected_crf=28.0),
            )
            record_event = execution._record_event

            def record_event_with_completion_failure(
                    event_connection: DBClient,
                    library_item_id: int,
                    event_type: str,
                    details: dict[str, Any],
            ) -> None:
                if event_type == "encoding_completed":
                    raise sqlite3.OperationalError("database is locked")
                record_event(event_connection, library_item_id, event_type, details)

            with self.assertLogs("mediaforce.encoding.manifest", level="WARNING"), patch(
                "mediaforce.execution._record_event",
                side_effect=record_event_with_completion_failure,
            ):
                self._encode_with_output_sizes(connection, item, quality, [5_100_000])

            artifact = self._staged_artifact(connection, item_id, staged_artifacts.c.staging_path)
            status = connection.execute(
                select(library_items.c.status).where(library_items.c.id == item_id)
            ).scalar_one()
            self.assertIsNotNone(artifact)
            self.assertTrue(staging_path.exists())
            self.assertEqual(status, "encoded")

    def _encode_with_output_sizes(
            self,
            connection: DBClient,
            item: dict[str, Any],
            quality: QualitySearchResult,
            output_sizes: list[int],
            *,
            retry_sample: SampleEncodeResult | None = None,
            retry_samples: list[SampleEncodeResult] | None = None,
            during_encode: Callable[[], None] | None = None,
            during_quality_search: Callable[[], None] | None = None,
            encode_command: list[str] | None = None,
            staged_width: int = 1920,
            staged_height: int = 1080,
            warm_start_plan: QualityWarmStartPlan | None = None,
            search_results: list[QualitySearchResult | Exception] | None = None,
            search_call_kwargs: list[dict[str, Any]] | None = None,
    ) -> tuple[list[Any], list[Any]]:
        sizes = list(output_sizes)
        queued_retry_samples = list(retry_samples or ([retry_sample] if retry_sample is not None else []))
        queued_quality_results: list[QualitySearchResult | Exception] = list(search_results or [quality])

        def run_encode_side_effect(*, temp_output: Path, **_: object) -> subprocess.CompletedProcess[str]:
            if during_encode is not None:
                during_encode()
            size = sizes.pop(0)
            temp_output.parent.mkdir(parents=True, exist_ok=True)
            temp_output.write_bytes(b"0" * size)
            return subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr="")

        staged_probe = ProbeSummary(
            duration_seconds=60.0,
            video_codec="av1",
            video_bitrate=900_000,
            width=staged_width,
            height=staged_height,
            pix_fmt="yuv420p10le",
            audio_track_count=0,
            subtitle_track_count=0,
            english_audio_count=0,
            english_subtitle_count=0,
            default_audio_language=None,
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )

        def measure_retry_side_effect(*_args: object, **_kwargs: object) -> SampleEncodeResult:
            if not queued_retry_samples:
                self.fail("Retry measurement was not expected")
            return queued_retry_samples.pop(0)

        def search_quality_side_effect(*_args: object, **kwargs: object) -> QualitySearchResult:
            if during_quality_search is not None:
                during_quality_search()
            if search_call_kwargs is not None:
                search_call_kwargs.append(dict(kwargs))
            result = queued_quality_results.pop(0) if len(queued_quality_results) > 1 else queued_quality_results[0]
            if isinstance(result, Exception):
                raise result
            return result

        resolved_encode_command = encode_command or [
            "ffmpeg",
            "-i",
            item["source_path"],
            "-c:v",
            "libsvtav1",
            "-pix_fmt",
            "yuv420p10le",
            "-preset",
            "4",
            "-svtav1-params",
            "tune=0:film-grain=0:film-grain-denoise=0",
            item["staging_path"],
        ]
        plan_context = (
            patch("mediaforce.encoding.manifest.plan_quality_warm_start", return_value=warm_start_plan)
            if warm_start_plan is not None
            else nullcontext()
        )
        with plan_context, patch("mediaforce.execution.resolve_item_source_path", return_value=Path(item["source_path"])), patch(
            "mediaforce.execution.resolve_item_staging_path", return_value=Path(item["staging_path"])
        ), patch("mediaforce.execution._search_quality", side_effect=search_quality_side_effect), patch(
            "mediaforce.execution._measure_quality_candidate", side_effect=measure_retry_side_effect
        ) as measure_mock, patch(
            "mediaforce.execution._select_streams",
            return_value={"audio_tracks": [], "subtitle_tracks": []},
        ), patch(
            "mediaforce.execution._build_ffmpeg_command",
            return_value=resolved_encode_command,
        ) as command_mock, patch(
            "mediaforce.execution._run_encode_command", side_effect=run_encode_side_effect
        ), patch("mediaforce.execution.probe_media", return_value=staged_probe), patch(
            "mediaforce.execution.file_fingerprint", return_value="staged-fingerprint"
        ):
            execution.encode_one_item(
                connection,
                self.config,
                self.root / "runs" / "manifest.json",
                {"run_id": "target-size-run", "items": [item]},
                0,
                item,
                overwrite=False,
            )
            return list(command_mock.call_args_list), list(measure_mock.call_args_list)

    def _attach_stream_budget(self, item: dict[str, Any]) -> None:
        item["stream_budget_ledger"] = execution.resolve_stream_budget_ledger(
            item,
            default_video_policy=item["resolved_policy"]["video"],
            output_container="mkv",
            prefer_persisted=False,
        ).to_payload()

    def _trace(
            self,
            item: dict[str, Any],
            *,
            selected_crf: float,
            retry_crf: float | None = None,
            retry_predicted_total: int = 4_800_000,
    ) -> dict[str, Any]:
        ledger = item["stream_budget_ledger"]
        selected = self._candidate(selected_crf, predicted_total=5_000_000)
        candidates = [self._candidate(24.0, predicted_total=5_800_000), selected]
        if retry_crf is not None:
            candidates.append(self._candidate(retry_crf, predicted_total=retry_predicted_total))
        return {
            "schema_version": 1,
            "status": "selected",
            "ledger": {
                "ledger_id": ledger["ledger_id"],
                "stream_plan_id": ledger["stream_plan"]["plan_id"],
            },
            "target": {
                "total_target_bytes": 5_000_000,
                "target_video_bytes": 1_000_000,
                "non_video_bytes": 4_000_000,
                "sample_projection_tolerance_percent": 10.0,
                "final_output_tolerance_percent": 5.0,
            },
            "source_cap": {"video_cap_bytes": 16_000_000},
            "quality_floor": {"metric": "VMAF", "target": 85.0, "minimum": 80.0},
            "candidates": candidates,
            "selected_candidate": selected,
        }

    def _warm_trace(self, item: dict[str, Any], *, selected_crf: float, status: str) -> dict[str, Any]:
        trace = self._trace(item, selected_crf=selected_crf)
        selected = trace["selected_candidate"]
        trace["candidates"] = [selected]
        trace["candidate_count"] = 1
        trace["warm_start"] = {
            "schema_version": 1,
            "status": status,
            "attempted": True,
            "requested_crf": selected_crf,
            "candidate_crf": int(selected_crf),
            "search_signature_id": "qms1_test",
            "cohort_id": "qsc1_test",
            "candidate_count": 1,
            "baseline_candidate_count": 0,
            "total_candidate_count": 1,
            "duration_seconds": 10.0,
            "fallback_used": status != "accepted",
            "fallback_reason": None,
            "candidate": selected,
        }
        return trace

    @staticmethod
    def _warm_start_plan(
            *,
            first_crf: float,
            block_reason: QualityWarmStartBlockReason | None = None,
            experiment_arm: str | None = None,
    ) -> QualityWarmStartPlan:
        recommendation = QualityShadowRecommendation(
            search_signature_id="qms1_test",
            evidence_cutoff_at="2026-07-25T12:00:00+00:00",
            first_crf=first_crf,
            scope="season",
            confidence="high",
            sample_count=10,
            cohort_id="qsc1_test",
            minimum_crf=first_crf,
            maximum_crf=first_crf,
            iqr=0.0,
            median_absolute_deviation=0.0,
            evidence_observation_ids=tuple(f"qso1_{index}" for index in range(10)),
            fallback_reason=None,
            reason="test",
            exclusions=(),
        )
        readiness = QualityShadowMetrics(
            evaluated_runs=10,
            recommendation_runs=10,
            coverage_rate=1.0,
            within_one_rate=1.0,
            false_narrow_rate=0.0,
            fallback_need_rate=0.0,
            median_candidate_savings_rate=0.8,
            median_search_time_savings_rate=0.8,
            passive_quality_floor_violations=0,
            active_quality_floor_violations=0,
            baseline_final_size_misses=0,
            active_final_size_misses=0,
            fallback_counts=(),
            performance_thresholds_met=block_reason is None,
            active_eligible=block_reason is None,
            blocking_reasons=(block_reason,) if block_reason is not None else (),
        )
        return QualityWarmStartPlan(
            recommendation=recommendation,
            readiness=readiness,
            candidate_crf=int(first_crf),
            adjusted=False,
            block_reason=block_reason,
            baseline_median_candidate_count=5.0,
            baseline_median_search_seconds=100.0,
            experiment_arm=(
                experiment_arm
                if experiment_arm is not None
                else "ineligible" if block_reason is not None
                else "passive"
            ),
            scope_prefix="tv/Example/Season 01",
        )

    @staticmethod
    def _candidate(crf: float, *, predicted_total: int) -> dict[str, Any]:
        return {
            "crf": crf,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 86.0,
            "quality_floor_met": True,
            "sampled_clip_bytes": int(crf * 100_000),
            "predicted_video_bytes": predicted_total - 4_000_000,
            "predicted_whole_episode_bytes": predicted_total,
            "target_distance_bytes": abs(predicted_total - 5_000_000),
        }

    def _manifest_item(self, item_id: int, source_path: Path, staging_path: Path) -> dict[str, Any]:
        return {
            "library_item_id": item_id,
            "resolved_policy": self._policy(),
            "rel_path": f"tv/show/{source_path.name}",
            "duration_seconds": 60.0,
            "video_codec": "h264",
            "video_bitrate": 8_000_000,
            "width": 1920,
            "height": 1080,
            "source_path": str(source_path),
            "source_fingerprint": f"fingerprint-{source_path.name}",
            "source_size_bytes": 20_000_000,
            "staging_path": str(staging_path),
            "output_container": "mkv",
            "audio_summary": [],
            "subtitle_summary": [],
            "attachment_summary": [],
        }

    @staticmethod
    def _policy() -> dict[str, Any]:
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
                "max_encoded_percent": 80,
                "target_size_bytes": 5_000_000,
                "target_size_mb": 5,
                "size_goal_mode": "absolute",
                "size_goal_source": "test",
                "sample_projection_tolerance_percent": 10,
                "final_output_tolerance_percent": 5,
                "default_grain": 0,
                "grain_denoise": 0,
            },
            "audio": {},
            "subtitle": {},
        }

    def _source_file(self, name: str) -> Path:
        path = self.root / "source" / "tv" / "show" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"source")
        return path

    def _staging_path(self, name: str) -> Path:
        return self.root / "staging" / "tv" / "show" / name

    def _insert_item(self, connection: DBClient, source_path: Path) -> int:
        now = "2026-07-11T00:00:00+00:00"
        result = connection.execute(
            library_items.insert().values(
                source_path=str(source_path),
                rel_path=f"tv/show/{source_path.name}",
                media_root="tv",
                parent_dir="tv/show",
                file_name=source_path.name,
                container=".mkv",
                size_bytes=20_000_000,
                mtime_ns=1,
                fingerprint=f"fingerprint-{source_path.name}",
                duration_seconds=60.0,
                video_codec="h264",
                video_bitrate=8_000_000,
                audio_summary_json="[]",
                subtitle_summary_json="[]",
                attachment_summary_json="[]",
                last_scan_id="scan-1",
                discovered_at=now,
                last_seen_at=now,
                updated_at=now,
                status="planned",
            )
        )
        return int(result.inserted_primary_key[0])

    @staticmethod
    def _staged_artifact(connection: DBClient, item_id: int, *columns: Any) -> Mapping[str, object] | None:
        return connection.execute(
            select(*columns).where(staged_artifacts.c.library_item_id == item_id)
        ).mappings().fetchone()

    @staticmethod
    def _events(connection: DBClient, item_id: int) -> list[dict[str, object]]:
        return [
            {str(key): value for key, value in dict(row).items()}
            for row in connection.execute(
                select(item_events.c.event_type, item_events.c.details_json)
                .where(item_events.c.library_item_id == item_id)
                .order_by(item_events.c.id.asc())
            ).mappings().fetchall()
        ]

    def _config(self) -> MediaforceConfig:
        paths = ConfigPaths(
            project_root=self.root,
            config_path=self.root / "config.toml",
            db_path=self.root / "library.sqlite3",
            run_manifest_dir=self.root / "runs",
            web_state_dir=self.root / "web",
            review_dir=self.root / "review",
            runtime_settings_path=self.root / "runtime.json",
        )
        return MediaforceConfig(
            raw={
                "media": {
                    "source_roots": {"tv": str(self.root / "source" / "tv")},
                    "staging_root": str(self.root / "staging"),
                    "archive_root": str(self.root / "archive"),
                },
                "remote_hosts": [],
            },
            paths=paths,
        )


if __name__ == "__main__":
    unittest.main()
