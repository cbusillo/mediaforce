import json
import sqlite3
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, cast
from unittest.mock import patch

from sqlalchemy import select

from mediaforce import execution
from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import item_events, library_items, staged_artifacts
from mediaforce.core.models import ProbeSummary
from mediaforce.encoding.quality import QualitySearchResult, SampleEncodeResult


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
            during_encode: Callable[[], None] | None = None,
            during_quality_search: Callable[[], None] | None = None,
    ) -> tuple[list[Any], list[Any]]:
        sizes = list(output_sizes)

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
            width=1920,
            height=1080,
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
            if retry_sample is None:
                self.fail("Retry measurement was not expected")
            return retry_sample

        def search_quality_side_effect(*_args: object, **_kwargs: object) -> QualitySearchResult:
            if during_quality_search is not None:
                during_quality_search()
            return quality

        with patch("mediaforce.execution.resolve_item_source_path", return_value=Path(item["source_path"])), patch(
            "mediaforce.execution.resolve_item_staging_path", return_value=Path(item["staging_path"])
        ), patch("mediaforce.execution._search_quality", side_effect=search_quality_side_effect), patch(
            "mediaforce.execution._measure_quality_candidate", side_effect=measure_retry_side_effect
        ) as measure_mock, patch(
            "mediaforce.execution._build_ffmpeg_command", return_value=["ffmpeg", "-i", item["source_path"], item["staging_path"]]
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
