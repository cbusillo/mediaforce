import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError

from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_migrations import _alembic_config, _alembic_script_location
from mediaforce.core.db_tables import (
    item_events,
    library_items,
    quality_search_observations,
    staged_artifacts,
)
from mediaforce.encoding.quality import QualitySearchResult
from mediaforce.tuning.quality_memory import QualitySearchContext
from mediaforce.tuning.quality_observations import (
    AUTHORITY_CORRECTION,
    AUTHORITY_STAGED_BACKFILL,
    MAX_QUALITY_OBSERVATION_CANDIDATES,
    MAX_QUALITY_OBSERVATION_FINAL_ATTEMPTS,
    QualitySearchObservation,
    append_quality_search_observation,
    backfill_quality_search_observations,
    build_failed_quality_observation,
    build_selected_quality_observation,
    load_current_quality_search_observations,
)


class QualityObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_context = open_db(Path(self.temp_dir.name) / "library.sqlite3")
        self.connection = self.db_context.__enter__()
        self.as_of = datetime(2026, 7, 24, 16, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.db_context.__exit__(None, None, None)
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_quality_success_is_structured_bounded_and_idempotent(self) -> None:
        stdout = "\n".join(
            f"crf {crf} VMAF {86 + crf / 100:.2f} predicted video stream size {500 - crf} MiB ({50 - crf / 10:.1f}%)"
            for crf in range(40)
        )
        result = QualitySearchResult(
            crf=39.0,
            metric="VMAF",
            target=85.0,
            score=86.39,
            stdout=stdout,
            quality_search_trace={
                "status": "selected",
                "configured_min_crf": 18,
                "configured_max_crf": 38,
                "search_max_crf": 63,
                "max_encoded_percent": 60,
                "attempts": [
                    {
                        "attempt": 1,
                        "metric_target": 85.5,
                        "max_crf": 38,
                        "status": "no_selection",
                        "candidates": [
                            {
                                "crf": crf,
                                "metric": "VMAF",
                                "metric_score": 86 + crf / 100,
                                "predicted_encode_percent": 50 - crf / 10,
                                "predicted_encode_size_bytes": (500 - crf) * 1024 ** 2,
                            }
                            for crf in range(10)
                        ],
                    },
                    {
                        "attempt": 2,
                        "metric_target": 85.0,
                        "max_crf": 63,
                        "status": "selected",
                        "candidates": [
                            {
                                "crf": crf,
                                "metric": "VMAF",
                                "metric_score": 86 + crf / 100,
                                "predicted_encode_percent": 50 - crf / 10,
                                "predicted_encode_size_bytes": (500 - crf) * 1024 ** 2,
                            }
                            for crf in range(10, 40)
                        ],
                    },
                ],
            },
        )
        observation = self._selected_observation(result=result)

        self.assertTrue(append_quality_search_observation(self.connection, observation))
        self.assertFalse(append_quality_search_observation(self.connection, observation))
        row = self.connection.execute(select(quality_search_observations)).mappings().one()
        trace = json.loads(row["candidate_trace_json"])

        self.assertEqual(row["candidate_count"], 40)
        self.assertEqual(trace["attempt_count"], 2)
        self.assertEqual(len(trace["candidates"]), MAX_QUALITY_OBSERVATION_CANDIDATES)
        self.assertTrue(trace["truncated"])
        self.assertNotIn("line", trace["candidates"][0])
        self.assertNotIn("predicted video stream size", row["candidate_trace_json"])
        self.assertEqual(row["search_signature_id"], self._context().signature_id)
        self.assertEqual(row["learning_eligible"], 1)

    def test_target_size_failure_is_diagnostic_only(self) -> None:
        trace = self._target_size_trace(status="quality_conflict", selected=False)
        trace["curve"] = {"shape": "non_monotonic", "raw_detail": "discard me"}
        trace["final_output_attempts"] = [
            {"status": "over_target", "actual_output_bytes": index, "raw_stdout": "discard me"}
            for index in range(10)
        ]
        observation = build_failed_quality_observation(
            search_run_id="qsr1_failure",
            library_item_id=1,
            source_rel_path="tv/Example/Season 01/Episode 01.mkv",
            source_fingerprint="source-fingerprint",
            context=self._target_context(),
            partial_context={},
            policy_hash="policy-hash",
            quality_metric="VMAF",
            search_objective="target_size",
            exclusion_reason="target_size_quality_conflict",
            target_size_trace=trace,
            quality_search_trace=None,
            quality_error_text=None,
            configured_min_crf=18,
            configured_max_crf=50,
            max_encoded_percent=60,
            search_started_at="2026-07-24T15:59:00+00:00",
            search_completed_at="2026-07-24T16:00:00+00:00",
            search_duration_seconds=60.0,
            provenance={"source": "test"},
            recorded_at="2026-07-24T16:00:00+00:00",
        )

        append_quality_search_observation(self.connection, observation)
        row = self.connection.execute(select(quality_search_observations)).mappings().one()

        self.assertEqual(row["outcome_kind"], "deterministic_search_failure")
        self.assertEqual(row["learning_eligible"], 0)
        self.assertEqual(row["exclusion_reason"], "target_size_quality_conflict")
        self.assertEqual(row["candidate_count"], 1)
        self.assertEqual(json.loads(row["bounds_json"])["search_max_crf"], 63)
        stored_trace = json.loads(row["candidate_trace_json"])
        self.assertEqual(len(stored_trace["final_output_attempts"]), MAX_QUALITY_OBSERVATION_FINAL_ATTEMPTS)
        self.assertNotIn("raw_detail", stored_trace["curve"])
        self.assertNotIn("raw_stdout", stored_trace["final_output_attempts"][0])

    def test_correction_appends_and_becomes_current(self) -> None:
        initial = self._selected_observation()
        append_quality_search_observation(self.connection, initial)
        corrected = self._selected_observation(
            result=QualitySearchResult(31.0, "VMAF", 85.0, 87.0, ""),
            authority=AUTHORITY_CORRECTION,
            revision=1,
            supersedes_observation_id=initial.observation_id,
            supersession_reason="corrected selected score",
        )

        append_quality_search_observation(self.connection, corrected)
        rows = self.connection.execute(select(quality_search_observations)).mappings().all()
        current = load_current_quality_search_observations(self.connection)

        self.assertEqual(len(rows), 2)
        self.assertEqual(current[0]["observation_id"], corrected.observation_id)
        self.assertEqual(current[0]["selected_crf"], 31.0)

    def test_database_rejects_update_and_delete(self) -> None:
        observation = self._selected_observation()
        append_quality_search_observation(self.connection, observation)
        self.connection.commit()

        with self.assertRaises(DatabaseError):
            self.connection.execute(
                text("UPDATE quality_search_observations SET selected_score = 99 WHERE observation_id = :id"),
                {"id": observation.observation_id},
            )
        self.connection.rollback()
        with self.assertRaises(DatabaseError):
            self.connection.execute(
                text("DELETE FROM quality_search_observations WHERE observation_id = :id"),
                {"id": observation.observation_id},
            )
        self.connection.rollback()

    def test_promoted_staged_success_backfill_is_lower_authority_and_idempotent(self) -> None:
        self._add_promoted_outcome()

        first = backfill_quality_search_observations(self.connection, as_of=self.as_of)
        second = backfill_quality_search_observations(self.connection, as_of=self.as_of)
        row = self.connection.execute(select(quality_search_observations)).mappings().one()

        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.existing, 1)
        self.assertEqual(row["authority"], AUTHORITY_STAGED_BACKFILL)
        self.assertEqual(row["search_run_id"], "qsr1_historical")
        self.assertIsNone(row["policy_hash"])
        self.assertEqual(row["learning_eligible"], 1)
        self.assertEqual(row["actual_output_bytes"], 500_000_000)

    def test_schema_upgrade_is_independent_from_restartable_backfill(self) -> None:
        db_path = Path(self.temp_dir.name) / "upgrade.sqlite3"
        with open_db(db_path) as connection:
            self._add_promoted_outcome(connection=connection)
        reset_engine_cache()
        with _alembic_script_location() as script_location:
            config = _alembic_config(db_path, script_location)
            command.downgrade(config, "20260723_0016")
            command.upgrade(config, "head")

        raw_connection = sqlite3.connect(db_path)
        try:
            observation_count = raw_connection.execute(
                "SELECT count(*) FROM quality_search_observations"
            ).fetchone()
            trigger_count = raw_connection.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name = 'quality_search_observations'"
            ).fetchone()
        finally:
            raw_connection.close()

        self.assertEqual(observation_count, (0,))
        self.assertEqual(trigger_count, (3,))
        with open_db(db_path) as connection:
            result = backfill_quality_search_observations(connection, as_of=self.as_of)
            self.assertEqual(result.inserted, 1)

    def _selected_observation(
            self,
            *,
            result: QualitySearchResult | None = None,
            authority: str = "runtime_native",
            revision: int = 0,
            supersedes_observation_id: str | None = None,
            supersession_reason: str | None = None,
    ) -> QualitySearchObservation:
        return build_selected_quality_observation(
            search_run_id="qsr1_success",
            library_item_id=1,
            source_rel_path="tv/Example/Season 01/Episode 01.mkv",
            source_fingerprint="source-fingerprint",
            context=self._context(),
            policy_hash="policy-hash",
            quality_result=result or QualitySearchResult(30.0, "VMAF", 85.0, 86.0, ""),
            configured_min_crf=18,
            configured_max_crf=50,
            max_encoded_percent=60,
            search_started_at="2026-07-24T15:59:00+00:00",
            search_completed_at="2026-07-24T16:00:00+00:00",
            search_duration_seconds=60.0,
            encode_completed_at="2026-07-24T17:00:00+00:00",
            encode_duration_seconds=3_600.0,
            actual_output_bytes=500_000_000,
            size_ratio=0.5,
            provenance={"source": "test"},
            recorded_at="2026-07-24T17:00:00+00:00",
            authority=authority,
            revision=revision,
            supersedes_observation_id=supersedes_observation_id,
            supersession_reason=supersession_reason,
        )

    def _add_promoted_outcome(self, *, connection: DBClient | None = None) -> None:
        db_connection = connection or self.connection
        context = self._context()
        rel_path = "tv/Example/Season 01/Episode 01.mkv"
        source_path = "/media/Episode 01.mkv"
        promoted_at = self.as_of - timedelta(days=1)
        validated_at = promoted_at - timedelta(minutes=1)
        staged_at = validated_at - timedelta(minutes=1)
        completed_at = staged_at - timedelta(minutes=1)
        db_connection.execute(
            library_items.insert().values(
                id=1,
                source_path=source_path,
                rel_path=rel_path,
                media_root="tv",
                parent_dir=str(PurePosixPath(rel_path).parent),
                file_name=PurePosixPath(rel_path).name,
                container="mkv",
                size_bytes=1_000_000_000,
                mtime_ns=1,
                fingerprint="library-fingerprint",
                duration_seconds=2_700.0,
                video_codec="h264",
                video_bitrate=4_000_000,
                width=1920,
                height=1080,
                pix_fmt="yuv420p",
                audio_track_count=1,
                subtitle_track_count=0,
                english_audio_count=1,
                english_subtitle_count=0,
                audio_summary_json="[]",
                subtitle_summary_json="[]",
                status="promoted",
                priority_score=0,
                last_scan_id="scan",
                discovered_at="2026-01-01T00:00:00+00:00",
                last_seen_at=self.as_of.isoformat(),
                updated_at=self.as_of.isoformat(),
            )
        )
        command = [
            "ffmpeg",
            "-i",
            source_path,
            "-c:v",
            context.encoder,
            "-pix_fmt",
            context.pixel_format,
            "-preset",
            context.preset,
            "-crf",
            "30",
            "-svtav1-params",
            str(context.encoder_parameters),
            "/staging/Episode 01.mkv",
        ]
        db_connection.execute(
            staged_artifacts.insert().values(
                library_item_id=1,
                manifest_run_id="manifest-run",
                item_index=0,
                encode_origin="queue",
                encode_job_id="job-id",
                encode_worker_id="worker-id",
                encode_host_key="host-id",
                source_path=source_path,
                source_rel_path=rel_path,
                source_size_bytes=1_000_000_000,
                source_duration_seconds=2_700.0,
                source_video_codec="h264",
                source_fingerprint="source-fingerprint",
                encode_completed_at=completed_at.isoformat(),
                encode_duration_seconds=3_600.0,
                staging_path="/staging/Episode 01.mkv",
                staging_size_bytes=500_000_000,
                size_ratio=0.5,
                chosen_crf=30.0,
                quality_metric="VMAF",
                quality_target=85.0,
                quality_score=86.0,
                encode_command_json=json.dumps(command),
                validation_json=json.dumps({"passed": True}),
                staged_at=staged_at.isoformat(),
                validated_at=validated_at.isoformat(),
                promoted_at=promoted_at.isoformat(),
                updated_at=promoted_at.isoformat(),
            )
        )
        db_connection.execute(
            item_events.insert().values(
                library_item_id=1,
                created_at=completed_at.isoformat(),
                event_type="encoding_completed",
                details_json=json.dumps(
                    {
                        "encode_completed_at": completed_at.isoformat(),
                        "source_rel_path": rel_path,
                        "chosen_crf": 30.0,
                        "quality_search_run_id": "qsr1_historical",
                        "target_size_trace": None,
                    }
                ),
            )
        )

    @staticmethod
    def _context() -> QualitySearchContext:
        return QualitySearchContext(
            metric="VMAF",
            target=85.0,
            minimum_quality_score=85.0,
            search_objective="quality",
            size_target_bytes=None,
            target_video_bitrate=None,
            source_codec="h264",
            output_width=1920,
            output_height=1080,
            encoder="libsvtav1",
            pixel_format="yuv420p10le",
            preset="4",
            encoder_parameters="tune=0:film-grain=8:film-grain-denoise=0",
            video_filter=None,
            output_container="mkv",
        )

    @staticmethod
    def _target_context() -> QualitySearchContext:
        return QualitySearchContext(
            metric="VMAF",
            target=85.0,
            minimum_quality_score=82.0,
            search_objective="target_size",
            size_target_bytes=300_000_000,
            target_video_bitrate=700_000,
            source_codec="h264",
            output_width=1920,
            output_height=1080,
            encoder="libsvtav1",
            pixel_format="yuv420p10le",
            preset="4",
            encoder_parameters="tune=0:film-grain=8:film-grain-denoise=0",
            video_filter=None,
            output_container="mkv",
        )

    @staticmethod
    def _target_size_trace(*, status: str, selected: bool) -> dict[str, object]:
        candidate = {
            "attempt": 1,
            "role": "target_seed",
            "crf": 52.0,
            "metric": "VMAF",
            "metric_target": 85.0,
            "metric_score": 84.0,
            "min_metric_score": 82.0,
            "quality_floor_met": True,
            "sampled_clip_bytes": 1_000_000,
            "predicted_video_bytes": 280_000_000,
            "predicted_whole_episode_bytes": 310_000_000,
            "predicted_encode_percent": 31.0,
            "predicted_encode_seconds": 100.0,
            "target_distance_bytes": 10_000_000,
            "within_sample_band": False,
            "violates_source_cap": False,
        }
        return {
            "status": status,
            "selection_reason": "test",
            "crf_bounds": {
                "min_crf": 18,
                "configured_max_crf": 50,
                "search_max_crf": 63,
            },
            "target": {"total_target_bytes": 300_000_000},
            "quality_floor": {"metric": "VMAF", "target": 85.0, "minimum": 82.0},
            "candidates": [candidate],
            "selected_candidate": candidate if selected else None,
            "best_reachable_candidate": candidate,
        }


if __name__ == "__main__":
    unittest.main()
