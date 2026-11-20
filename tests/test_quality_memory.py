import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from sqlalchemy import event

from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import item_events, library_items, staged_artifacts
from mediaforce.tuning.quality_memory import (
    QualitySearchContext,
    accepted_quality_outcomes,
)


class QualityMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_context = open_db(Path(self.temp_dir.name) / "library.sqlite3")
        self.connection = self.db_context.__enter__()
        self.as_of = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.db_context.__exit__(None, None, None)
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_signature_is_stable_and_sensitive_to_encode_assumptions(self) -> None:
        baseline = self._context()
        normalized = QualitySearchContext(
            metric="vmaf",
            target=85,
            minimum_quality_score=85,
            search_objective="quality",
            size_target_bytes=None,
            target_video_bitrate=None,
            source_codec="H264",
            output_width=1920,
            output_height=1080,
            encoder="LIBSVTAV1",
            pixel_format="YUV420P10LE",
            preset="4",
            encoder_parameters="tune=0:film-grain=8:film-grain-denoise=0",
            video_filter=None,
            output_container=".MKV",
        )

        self.assertEqual(baseline.signature_id, normalized.signature_id)
        for changed in (
            replace(baseline, target=86.0),
            replace(baseline, output_width=1280),
            replace(baseline, encoder="libx265"),
            replace(baseline, preset="5"),
            replace(baseline, video_filter="scale=-2:720"),
            replace(baseline, compression_intent_id="ci1_transparent"),
        ):
            self.assertNotEqual(baseline.signature_id, changed.signature_id)

    def test_warm_selected_completion_is_not_reintroduced_by_historical_loader(self) -> None:
        context = self._context()
        rel_path = "tv/Example Show/Season 01/Episode 01.mkv"
        self._add_outcome(
            1,
            rel_path,
            crf=30.0,
            context=context,
            warm_start_status="accepted",
        )

        outcomes, exclusions = accepted_quality_outcomes(self.connection, metric=context.metric, as_of=self.as_of)

        self.assertFalse(outcomes)
        self.assertEqual(exclusions["warm_start_selected"], 1)

    def test_accepted_outcomes_keep_the_measured_search_objective(self) -> None:
        quality_context = self._context()
        target_size_context = replace(
            quality_context,
            minimum_quality_score=80.0,
            search_objective="target_size",
            size_target_bytes=225_000_000,
            target_video_bitrate=533_000,
        )
        for item_id, context in enumerate((quality_context, target_size_context), start=1):
            self._add_outcome(item_id, f"tv/Show/Season 01/{item_id}.mkv", crf=40.0, context=context)

        outcomes, _ = accepted_quality_outcomes(self.connection, metric=quality_context.metric, as_of=self.as_of)
        self.assertCountEqual([outcome.context for outcome in outcomes], [quality_context, target_size_context])

    def test_historical_rows_fail_closed_with_typed_exclusions(self) -> None:
        context = self._context()
        self._add_outcome(
            1,
            "tv/Example Show/Season 01/Valid.mkv",
            crf=30.0,
            context=context,
            legacy_event=True,
        )
        self._add_outcome(2, "tv/Example Show/Season 01/Unpromoted.mkv", crf=31.0, context=context, promoted=False)
        self._add_outcome(3, "tv/Example Show/Season 01/Calibration.mkv", crf=31.0, context=context, origin="calibration")
        self._add_outcome(4, "tv/Example Show/Season 01/Invalid.mkv", crf=31.0, context=context, validation_passed=False)
        self._add_outcome(5, "tv/Example Show/Season 01/Stale markers.mkv", crf=31.0, context=context, stale_order=True)
        self._add_outcome(6, "tv/Example Show/Season 01/Re-encoded.mkv", crf=31.0, context=context, status="encoded")
        self._add_outcome(7, "tv/Example Show/Season 01/Changed.mkv", crf=31.0, context=context, content_changed_after=True)
        self._add_outcome(8, "tv/Example Show/Season 01/Old.mkv", crf=31.0, context=context, promoted_days_ago=181)
        self._add_outcome(9, "tv/Example Show/Season 01/Malformed.mkv", crf=31.0, context=context, malformed_command=True)
        self._add_outcome(10, "tv/Example Show/Season 01/Replaced.mkv", crf=31.0, context=context, artifact_changed_after=True)
        self._add_outcome(11, "tv/Example Show/Season 01/No event.mkv", crf=31.0, context=context, event_present=False)
        self._add_outcome(12, "tv/Example Show/Season 01/Below floor.mkv", crf=31.0, context=context, quality_score=80.0)

        outcomes, exclusions = accepted_quality_outcomes(self.connection, metric=context.metric, as_of=self.as_of)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(exclusions["unsupported_encode_origin"], 1)
        self.assertEqual(exclusions["validation_not_passed"], 1)
        self.assertEqual(exclusions["acceptance_timestamp_order_invalid"], 1)
        self.assertEqual(exclusions["library_item_not_promoted"], 1)
        self.assertEqual(exclusions["content_changed_after_acceptance"], 1)
        self.assertEqual(exclusions["accepted_outcome_too_old"], 1)
        self.assertEqual(exclusions["encode_context_missing"], 1)
        self.assertEqual(exclusions["artifact_changed_after_acceptance"], 1)
        self.assertEqual(exclusions["completion_event_missing"], 1)
        self.assertEqual(exclusions["quality_floor_not_met"], 1)

    def test_loader_executes_selects_only_and_launches_no_work(self) -> None:
        context = self._context()
        for item_id in range(1, 5):
            self._add_outcome(
                item_id,
                f"tv/Example Show/Season 01/Episode {item_id:02d}.mkv",
                crf=30.0 + item_id,
                context=context,
            )
        self.connection.commit()
        statements: list[str] = []

        def capture_statement(
                _connection: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
        ) -> None:
            statements.append(statement.lstrip().partition(" ")[0].upper())

        event.listen(self.connection.engine, "before_cursor_execute", capture_statement)
        try:
            with patch("subprocess.run", side_effect=AssertionError("unexpected subprocess")) as run_mock, patch(
                "subprocess.Popen",
                side_effect=AssertionError("unexpected subprocess"),
            ) as popen_mock:
                outcomes, _ = accepted_quality_outcomes(self.connection, metric=context.metric, as_of=self.as_of)
        finally:
            event.remove(self.connection.engine, "before_cursor_execute", capture_statement)

        self.assertEqual(len(outcomes), 4)
        self.assertEqual(statements, ["SELECT"])
        run_mock.assert_not_called()
        popen_mock.assert_not_called()

    def test_new_cli_origins_preserve_promoted_quality_learning(self) -> None:
        context = self._context()
        for item_id, origin in enumerate(("cli-production", "cli-review"), start=1):
            with self.subTest(origin=origin):
                rel_path = f"tv/Example Show/Season 01/CLI {item_id}.mkv"
                self._add_outcome(item_id, rel_path, crf=30.0, context=context, origin=origin, source_fingerprint=origin)
                outcomes, _ = accepted_quality_outcomes(self.connection, metric=context.metric, as_of=self.as_of)
                self.assertIn(rel_path, [outcome.rel_path for outcome in outcomes])

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

    def _add_outcome(
            self,
            item_id: int,
            rel_path: str,
            *,
            crf: float,
            context: QualitySearchContext,
            source_fingerprint: str | None = None,
            quality_score: float = 86.0,
            origin: str = "queue",
            promoted: bool = True,
            validation_passed: bool = True,
            stale_order: bool = False,
            status: str = "promoted",
            content_changed_after: bool = False,
            promoted_days_ago: int = 10,
            malformed_command: bool = False,
            artifact_changed_after: bool = False,
            event_present: bool = True,
            legacy_event: bool = False,
            warm_start_status: str | None = None,
    ) -> None:
        promoted_at = self.as_of - timedelta(days=promoted_days_ago)
        validated_at = promoted_at - timedelta(minutes=1)
        staged_at = validated_at - timedelta(minutes=1)
        encode_completed_at = staged_at - timedelta(minutes=1)
        if stale_order:
            encode_completed_at = promoted_at + timedelta(minutes=1)
        content_version_changed_at = (
            promoted_at + timedelta(minutes=1)
            if content_changed_after
            else None
        )
        source_path = f"/media/source-{item_id}.mkv"
        self.connection.execute(
            library_items.insert().values(
                id=item_id,
                source_path=source_path,
                rel_path=rel_path,
                media_root=PurePosixPath(rel_path).parts[0],
                parent_dir=str(PurePosixPath(rel_path).parent),
                file_name=PurePosixPath(rel_path).name,
                container="mkv",
                size_bytes=1_000_000_000,
                mtime_ns=item_id,
                fingerprint=f"library-fingerprint-{item_id}",
                duration_seconds=2_700.0,
                video_codec=context.source_codec,
                video_bitrate=4_000_000,
                width=context.output_width,
                height=context.output_height,
                pix_fmt="yuv420p",
                audio_track_count=1,
                subtitle_track_count=0,
                english_audio_count=1,
                english_subtitle_count=0,
                audio_summary_json="[]",
                subtitle_summary_json="[]",
                content_version_changed_at=(
                    content_version_changed_at.isoformat()
                    if content_version_changed_at is not None
                    else None
                ),
                status=status,
                priority_score=0,
                last_scan_id="scan",
                discovered_at="2026-01-01T00:00:00+00:00",
                last_seen_at=self.as_of.isoformat(),
                updated_at=self.as_of.isoformat(),
            )
        )
        command = self._encode_command(source_path, context, crf, item_id)
        self.connection.execute(
            staged_artifacts.insert().values(
                library_item_id=item_id,
                encode_origin=origin,
                source_path=source_path,
                source_rel_path=rel_path,
                source_size_bytes=1_000_000_000,
                source_duration_seconds=2_700.0,
                source_video_codec=context.source_codec,
                source_fingerprint=source_fingerprint or f"source-fingerprint-{item_id}",
                encode_completed_at=encode_completed_at.isoformat(),
                staging_path=f"/nonexistent/staging-{item_id}.{context.output_container}",
                staging_size_bytes=500_000_000,
                chosen_crf=crf,
                quality_metric=context.metric,
                quality_target=context.target,
                quality_score=quality_score,
                encode_command_json=("{" if malformed_command else json.dumps(command)),
                validation_json=json.dumps({"passed": validation_passed}),
                staged_at=staged_at.isoformat(),
                validated_at=validated_at.isoformat(),
                promoted_at=promoted_at.isoformat() if promoted else None,
                updated_at=(
                    (promoted_at + timedelta(minutes=1)).isoformat()
                    if artifact_changed_after
                    else (promoted_at if promoted else staged_at).isoformat()
                ),
            )
        )
        if event_present:
            event_details: dict[str, object] = {
                "encode_completed_at": encode_completed_at.isoformat(),
                "source_rel_path": rel_path,
                "chosen_crf": crf,
                "quality_metric": context.metric,
                "quality_target": context.target,
                "quality_score": quality_score,
            }
            if not legacy_event:
                event_details["target_size_trace"] = self._target_size_trace(context)
                event_details["compression_intent_id"] = context.compression_intent_id
            if warm_start_status is not None:
                event_details["quality_warm_start"] = {"status": warm_start_status}
            self.connection.execute(
                item_events.insert().values(
                    library_item_id=item_id,
                    created_at=encode_completed_at.isoformat(),
                    event_type="encoding_completed",
                    details_json=json.dumps(event_details),
                )
            )

    @staticmethod
    def _encode_command(
            source_path: str,
            context: QualitySearchContext,
            crf: float,
            item_id: int,
    ) -> list[str]:
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
            str(crf),
        ]
        if context.encoder_parameters is not None:
            command.extend(["-svtav1-params", context.encoder_parameters])
        if context.video_filter is not None:
            command.extend(["-vf", context.video_filter])
        command.append(f"/nonexistent/output-{item_id}.{context.output_container}")
        return command

    @staticmethod
    def _target_size_trace(context: QualitySearchContext) -> dict[str, object] | None:
        if context.search_objective != "target_size":
            return None
        assert context.size_target_bytes is not None
        assert context.target_video_bitrate is not None
        target_video_bytes = int(context.target_video_bitrate * 2_700 / 8)
        return {
            "target": {
                "total_target_bytes": context.size_target_bytes,
                "target_video_bytes": target_video_bytes,
            },
            "quality_floor": {
                "metric": context.metric,
                "target": context.target,
                "minimum": context.minimum_quality_score,
            },
        }


if __name__ == "__main__":
    unittest.main()
