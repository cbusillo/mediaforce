import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mediaforce import execution
from mediaforce.encoding import quality_search
from mediaforce.encoding.quality import QualitySearchResult, SampleEncodeResult
from mediaforce.tuning.size_goals import SizeGoalIntent, megabytes_to_bytes
from mediaforce.tuning.stream_budget import (
    StreamBudgetIdentityError,
    StreamBudgetLedger,
    resolve_stream_budget_ledger,
    stream_budget_projection_blocker,
)


class StreamBudgetTests(unittest.TestCase):
    def test_copied_and_transcoded_audio_layouts_use_the_production_plan(self) -> None:
        cases = (
            ("copy", 2, "aac", 192_000, 192_000),
            ("copy", 6, "aac", 640_000, 640_000),
            ("copy", 8, "aac", 768_000, 768_000),
            ("transcode", 2, "eac3", 640_000, 128_000),
            ("transcode", 6, "eac3", 640_000, 256_000),
            ("transcode", 8, "eac3", 768_000, 320_000),
        )
        for action, channels, codec, source_bitrate, expected_bitrate in cases:
            with self.subTest(action=action, channels=channels):
                ledger = self._ledger(
                    audio={
                        "index": 1,
                        "codec_name": codec,
                        "channels": channels,
                        "language": "eng",
                        "default": 1,
                        "bit_rate": source_bitrate,
                    }
                )

                audio_plan = ledger.stream_plan.audio_streams[0]
                audio_entry = next(entry for entry in ledger.entries if entry.category == "audio")
                self.assertEqual(audio_plan.action, action)
                self.assertEqual(audio_entry.bitrate_bps, expected_bitrate)
                self.assertEqual(audio_entry.budget_bytes, expected_bitrate * 3600 // 8)
                self.assertEqual(audio_entry.provenance, "production_audio_policy_bitrate" if action == "transcode" else "ffprobe_stream_bitrate_times_runtime")

    def test_unknown_copied_audio_bitrate_uses_visible_channel_fallback(self) -> None:
        stereo = self._ledger(
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 2,
                "language": "eng",
                "default": 1,
            }
        )
        surround = self._ledger(
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 6,
                "language": "eng",
                "default": 1,
            }
        )

        stereo_entry = next(entry for entry in stereo.entries if entry.category == "audio")
        surround_entry = next(entry for entry in surround.entries if entry.category == "audio")
        self.assertEqual(stereo_entry.bitrate_bps, 192_000)
        self.assertEqual(surround_entry.bitrate_bps, 640_000)
        self.assertEqual(stereo_entry.provenance, "channel_layout_copy_bitrate_fallback")
        self.assertEqual(stereo_entry.confidence, "low")
        self.assertTrue(stereo_entry.requires_measurement)
        self.assertEqual(stereo.feasibility_status, "requires_measurement")

    def test_text_image_subtitles_attachments_and_multiple_tracks_are_budgeted(self) -> None:
        ledger = self._ledger(
            audio={
                "index": 1,
                "codec_name": "eac3",
                "channels": 6,
                "language": "eng",
                "default": 1,
                "bit_rate": 640_000,
            },
            subtitles=[
                {"index": 3, "codec_name": "hdmv_pgs_subtitle", "language": "eng", "forced": 1, "size_bytes": 2_500_000},
                {"index": 2, "codec_name": "subrip", "language": "eng", "default": 1},
                {"index": 4, "codec_name": "subrip", "language": "jpn"},
            ],
            attachments=[
                {"index": 5, "codec_name": "ttf", "file_name": "font.ttf", "size_bytes": 250_000},
                {"index": 6, "codec_name": "otf", "file_name": "font.otf", "size_bytes": 500_000},
            ],
        )

        self.assertEqual([stream.source_index for stream in ledger.stream_plan.subtitle_streams], [2, 3])
        subtitle_entries = [entry for entry in ledger.entries if entry.category == "subtitle"]
        attachment_entries = [entry for entry in ledger.entries if entry.category == "attachment"]
        self.assertEqual([entry.action for entry in subtitle_entries], ["transcode", "copy"])
        self.assertEqual(subtitle_entries[0].budget_bytes, 128 * 1024)
        self.assertEqual(subtitle_entries[1].budget_bytes, 2_500_000)
        self.assertEqual(sum(entry.budget_bytes or 0 for entry in attachment_entries), 750_000)
        self.assertTrue(ledger.stream_plan.copies_attachments)

    def test_unknown_image_subtitle_and_attachment_sizes_require_measurement(self) -> None:
        ledger = self._ledger(
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 2,
                "language": "eng",
                "default": 1,
                "bit_rate": 192_000,
            },
            subtitles=[{"index": 2, "codec_name": "hdmv_pgs_subtitle", "language": "eng"}],
            attachments=[{"index": 3, "codec_name": "ttf", "file_name": "font.ttf"}],
        )

        image_entry = next(entry for entry in ledger.entries if entry.category == "subtitle")
        attachment_entry = next(entry for entry in ledger.entries if entry.category == "attachment")
        self.assertEqual(image_entry.provenance, "image_subtitle_copy_fallback")
        self.assertEqual(attachment_entry.provenance, "attachment_copy_fallback")
        self.assertGreater(image_entry.upper_bound_bytes or 0, image_entry.budget_bytes or 0)
        self.assertGreater(attachment_entry.upper_bound_bytes or 0, attachment_entry.budget_bytes or 0)
        self.assertEqual(ledger.feasibility_status, "requires_measurement")

    def test_nonpositive_video_budget_is_arithmetically_infeasible(self) -> None:
        ledger = self._ledger(
            target_bytes=100_000_000,
            audio={
                "index": 1,
                "codec_name": "eac3",
                "channels": 6,
                "language": "eng",
                "default": 1,
                "bit_rate": 640_000,
            },
        )

        self.assertEqual(ledger.audio_bytes, 115_200_000)
        self.assertLess(ledger.remaining_video_bytes or 0, 0)
        self.assertEqual(ledger.feasibility_status, "arithmetically_infeasible")
        self.assertTrue(ledger.arithmetic_infeasible)

    def test_container_reservation_can_make_budget_infeasible(self) -> None:
        ledger = self._ledger(
            target_bytes=3_000_000,
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 2,
                "language": "eng",
                "default": 1,
                "size_bytes": 1,
            },
            duration_seconds=1.0,
        )

        self.assertEqual(ledger.container_bytes, 4_000_000)
        self.assertEqual(ledger.remaining_video_bytes, -1_000_001)
        self.assertEqual(ledger.feasibility_status, "arithmetically_infeasible")

    def test_small_positive_budget_remains_measurable_without_source_ratio_judgment(self) -> None:
        ledger = self._ledger(
            target_bytes=250_000_000,
            source_size_bytes=4_000_000_000,
            audio={
                "index": 1,
                "codec_name": "eac3",
                "channels": 2,
                "language": "eng",
                "default": 1,
                "bit_rate": 192_000,
            },
        )

        self.assertGreater(ledger.remaining_video_bytes or 0, 0)
        self.assertEqual(ledger.feasibility_status, "feasible")
        self.assertEqual(ledger.feasibility_reasons, ())

    def test_missing_runtime_keeps_bitrate_unknown(self) -> None:
        ledger = self._ledger(
            duration_seconds=None,
            audio={
                "index": 1,
                "codec_name": "eac3",
                "channels": 2,
                "language": "eng",
                "default": 1,
            },
        )

        self.assertIsNone(ledger.audio_bytes)
        self.assertIsNone(ledger.remaining_video_bytes)
        self.assertIsNone(ledger.remaining_video_bitrate_bps)
        self.assertEqual(ledger.feasibility_status, "requires_measurement")

    def test_decimal_target_bytes_and_container_percent_are_exact(self) -> None:
        target_bytes = megabytes_to_bytes(600.000001)
        ledger = self._ledger(
            target_bytes=target_bytes,
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 2,
                "language": "eng",
                "default": 1,
                "size_bytes": 10_000_000,
            },
        )

        self.assertEqual(target_bytes, 600_000_001)
        self.assertEqual(ledger.total_target_bytes, 600_000_001)
        self.assertEqual(ledger.container_bytes, 6_000_001)
        self.assertEqual(ledger.remaining_video_bytes, 584_000_000)

    def test_source_relative_cap_subtracts_non_video_before_search(self) -> None:
        ledger = self._ledger(
            source_size_bytes=1_000_000_000,
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 2,
                "language": "eng",
                "default": 1,
                "size_bytes": 100_000_000,
            },
        )

        self.assertEqual(ledger.source_cap_total_bytes, 800_000_000)
        self.assertEqual(ledger.source_cap_video_bytes, 696_000_000)
        self.assertEqual(ledger.source_cap_video_percent, 69.6)

    def test_projection_blocks_target_band_above_source_cap(self) -> None:
        ledger = self._ledger(
            target_bytes=600_000_000,
            source_size_bytes=360_000_000,
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 2,
                "language": "eng",
                "default": 1,
                "size_bytes": 12_000_000,
            },
        )

        blocker = stream_budget_projection_blocker(ledger)

        self.assertIsNotNone(blocker)
        self.assertEqual(blocker.code, "target_lower_bound_exceeds_source_relative_cap")
        self.assertEqual(blocker.target_lower_bound_bytes, 540_000_000)
        self.assertEqual(blocker.source_cap_total_bytes, 288_000_000)
        self.assertIn("80% source cap", blocker.message)

    def test_projection_allows_target_band_below_source_cap(self) -> None:
        ledger = self._ledger(
            target_bytes=600_000_000,
            source_size_bytes=800_000_000,
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 2,
                "language": "eng",
                "default": 1,
                "size_bytes": 12_000_000,
            },
        )

        self.assertIsNone(stream_budget_projection_blocker(ledger))

    def test_projection_blocks_target_consumed_by_non_video_budget(self) -> None:
        ledger = self._ledger(
            target_bytes=10_000_000,
            source_size_bytes=1_000_000_000,
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 2,
                "language": "eng",
                "default": 1,
                "size_bytes": 12_000_000,
            },
        )

        blocker = stream_budget_projection_blocker(ledger)

        self.assertIsNotNone(blocker)
        self.assertTrue(blocker.code.startswith("target_consumed_by_"))
        self.assertIn("leaves no room for video", blocker.message)

    def test_projection_blocks_source_cap_consumed_by_non_video_budget(self) -> None:
        ledger = self._ledger(
            target_bytes=600_000_000,
            source_size_bytes=100_000_000,
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 2,
                "language": "eng",
                "default": 1,
                "size_bytes": 100_000_000,
            },
        )

        blocker = stream_budget_projection_blocker(ledger)

        self.assertIsNotNone(blocker)
        self.assertEqual(blocker.code, "source_relative_cap_consumed_by_non_video_budget")
        self.assertIn("consume the 80% source cap", blocker.message)

    def test_ledger_round_trip_rejects_stale_policy(self) -> None:
        ledger = self._ledger(
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 2,
                "language": "eng",
                "default": 1,
                "bit_rate": 192_000,
            },
        )
        restored = StreamBudgetLedger.from_payload(ledger.to_payload())
        self.assertEqual(restored.ledger_id, ledger.ledger_id)
        self.assertEqual(restored.stream_plan.plan_id, ledger.stream_plan.plan_id)

        stale_item = self._item(
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 2,
                "language": "eng",
                "default": 1,
                "bit_rate": 192_000,
            }
        )
        stale_item["stream_budget_ledger"] = ledger.to_payload()
        stale_item["resolved_policy"]["audio"]["stereo_opus_bitrate"] = "112k"
        with self.assertRaises(StreamBudgetIdentityError):
            resolve_stream_budget_ledger(stale_item)

    def test_sample_search_and_production_command_share_the_ledger_plan(self) -> None:
        ledger = self._ledger(
            audio={
                "index": 1,
                "codec_name": "eac3",
                "channels": 6,
                "language": "eng",
                "default": 1,
                "bit_rate": 640_000,
            },
            subtitles=[{"index": 2, "codec_name": "subrip", "language": "eng", "default": 1}],
            attachments=[{"index": 3, "codec_name": "ttf", "file_name": "font.ttf", "size_bytes": 250_000}],
        )
        run_crf_search = Mock(
            return_value=QualitySearchResult(crf=28.0, metric="VMAF", target=85.0, score=85.2, stdout="ok")
        )
        measured_crfs: list[int] = []

        def run_sample_encode(_path: Path, *, crf: float, **_kwargs: object) -> SampleEncodeResult:
            crf_int = int(crf)
            measured_crfs.append(crf_int)
            predicted_video_bytes = 620_000_000 - 14_000_000 * crf_int
            return SampleEncodeResult(
                metric="VMAF",
                score=90.0,
                predicted_encode_percent=predicted_video_bytes / 40_000_000,
                predicted_encode_seconds=30.0,
                predicted_encode_size_bytes=predicted_video_bytes,
                stdout=f"crf {crf_int}",
                sampled_clip_size_bytes=crf_int * 100_000,
            )

        quality_result = quality_search.search_quality(
            Path("/tmp/input.mkv"),
            self._policy()["video"],
            stream_budget_ledger=ledger,
            host_media_access_for_host=Mock(return_value="mounted"),
            select_quality_metric=Mock(return_value=("vmaf", 95.0)),
            build_svt_params=Mock(return_value=[]),
            effective_video_preset=Mock(return_value=4),
            run_crf_search=run_crf_search,
            run_sample_encode=run_sample_encode,
        )
        with patch("mediaforce.execution.ffmpeg_binary", return_value="ffmpeg"), patch(
            "mediaforce.execution.ffmpeg_hwaccel_input_args", return_value=[]
        ):
            command = execution._build_ffmpeg_command(
                source_path=Path("/tmp/input.mkv"),
                staging_path=Path("/tmp/output.mkv"),
                source_codec="h264",
                video_policy=self._policy()["video"],
                preset=4,
                audio_policy=self._policy()["audio"],
                subtitle_policy=self._policy()["subtitle"],
                selection=ledger.stream_plan,
                quality=quality_result,
            )

        self.assertFalse(run_crf_search.called)
        self.assertEqual(measured_crfs[0], 37)
        self.assertEqual(quality_result.target_size_trace["ledger"]["stream_plan_id"], ledger.stream_plan.plan_id)
        self.assertEqual(quality_result.target_size_trace["selected_candidate"]["sampled_clip_bytes"], 3_200_000)
        self.assertIn("0:1", command)
        self.assertIn("0:2", command)
        self.assertIn("0:t?", command)
        self.assertIn("-c:t", command)
        self.assertEqual(ledger.stream_plan.plan_id, StreamBudgetLedger.from_payload(ledger.to_payload()).stream_plan.plan_id)

    def test_target_search_ceiling_is_explicit_and_legacy_jobs_keep_their_saved_bound(self) -> None:
        ledger = self._ledger(
            audio={
                "index": 1,
                "codec_name": "aac",
                "channels": 2,
                "language": "eng",
                "default": 1,
                "bit_rate": 128_000,
            }
        )
        expected = QualitySearchResult(
            crf=45.0,
            metric="VMAF",
            target=85.0,
            score=91.0,
            stdout="target-size-search",
        )
        for configured_ceiling, expected_ceiling in ((63, 63), (None, 38)):
            policy = dict(self._policy()["video"])
            if configured_ceiling is not None:
                policy["target_search_max_crf"] = configured_ceiling
            with self.subTest(configured_ceiling=configured_ceiling), patch(
                "mediaforce.encoding.quality_search.search_target_size",
                return_value=expected,
            ) as search_target_size_mock:
                result = quality_search.search_quality(
                    Path("/tmp/input.mkv"),
                    policy,
                    stream_budget_ledger=ledger,
                    host_media_access_for_host=Mock(return_value="mounted"),
                    select_quality_metric=Mock(return_value=("vmaf", 85.0)),
                    build_svt_params=Mock(return_value=[]),
                    effective_video_preset=Mock(return_value=4),
                    run_crf_search=Mock(),
                    run_sample_encode=Mock(),
                )

                self.assertIs(result, expected)
                self.assertEqual(search_target_size_mock.call_args.kwargs["search_max_crf"], expected_ceiling)

    def test_retry_measurement_reuses_quality_search_context(self) -> None:
        sample = SampleEncodeResult(
            metric="VMAF",
            score=93.4,
            predicted_encode_percent=30.5,
            predicted_encode_seconds=120.0,
            predicted_encode_size_bytes=240_000_000,
            stdout="measured",
        )
        run_sample_encode = Mock(return_value=sample)
        build_svt_params = Mock(return_value=["tune=0"])
        effective_video_preset = Mock(return_value=5)

        result = quality_search.measure_quality_candidate(
            Path("/tmp/input.mkv"),
            self._policy()["video"],
            crf=36.0,
            source_codec="h264",
            width=3840,
            height=2160,
            host={"mode": "ssh", "media_access": "stream"},
            quality_temp_dir=Path("/tmp/quality"),
            host_media_access_for_host=Mock(return_value="stream"),
            select_quality_metric=Mock(return_value=("vmaf", 95.0)),
            build_svt_params=build_svt_params,
            effective_video_preset=effective_video_preset,
            run_sample_encode=run_sample_encode,
        )

        self.assertIs(result, sample)
        build_svt_params.assert_called_once()
        effective_video_preset.assert_called_once_with(self._policy()["video"], width=3840, height=2160)
        call = run_sample_encode.call_args
        self.assertEqual(call.args, (Path("/tmp/input.mkv"),))
        self.assertEqual(call.kwargs["crf"], 36.0)
        self.assertEqual(call.kwargs["preferred_metric"], "vmaf")
        self.assertEqual(call.kwargs["preset"], 5)
        self.assertEqual(call.kwargs["svt_params"], ["tune=0"])
        self.assertEqual(call.kwargs["host"]["mode"], "local")
        self.assertEqual(call.kwargs["quality_temp_dir"], Path("/tmp/quality"))

    def _ledger(
            self,
            *,
            audio: dict[str, object],
            subtitles: list[dict[str, object]] | None = None,
            attachments: list[dict[str, object]] | None = None,
            target_bytes: int = 300_000_000,
            duration_seconds: float | None = 3600.0,
            source_size_bytes: int = 4_000_000_000,
    ) -> StreamBudgetLedger:
        item = self._item(
            audio=audio,
            subtitles=subtitles,
            attachments=attachments,
            duration_seconds=duration_seconds,
            source_size_bytes=source_size_bytes,
        )
        size_goal = SizeGoalIntent(
            mode="absolute",
            value_bytes=target_bytes,
            reference_runtime_seconds=None,
            sample_projection_tolerance_percent=10.0,
            final_output_tolerance_percent=5.0,
            source="test",
        ).resolve(duration_seconds)
        return resolve_stream_budget_ledger(
            item,
            resolved_size_goal=size_goal,
            prefer_persisted=False,
        )

    def _item(
            self,
            *,
            audio: dict[str, object],
            subtitles: list[dict[str, object]] | None = None,
            attachments: list[dict[str, object]] | None = None,
            duration_seconds: float | None = 3600.0,
            source_size_bytes: int = 4_000_000_000,
    ) -> dict[str, object]:
        return {
            "library_item_id": 7,
            "rel_path": "tv/show/episode.mkv",
            "source_fingerprint": "fingerprint-7",
            "source_size_bytes": source_size_bytes,
            "video_bitrate": 8_000_000,
            "duration_seconds": duration_seconds,
            "output_container": "mkv",
            "resolved_policy": self._policy(),
            "audio_summary": [audio],
            "subtitle_summary": subtitles or [],
            "attachment_summary": attachments if attachments is not None else [],
        }

    @staticmethod
    def _policy() -> dict[str, object]:
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
                "target_relax_step_vmaf": 0.5,
                "sample_every": "8m",
                "sample_duration": "20s",
                "min_crf": 18,
                "max_crf": 38,
                "max_encoded_percent": 80,
                "default_grain": 0,
                "grain_denoise": 0,
            },
            "audio": {
                "copy_codecs": ["aac", "opus"],
                "convert_to_opus_codecs": ["eac3"],
                "stereo_opus_bitrate": "128k",
                "surround_5_1_opus_bitrate": "256k",
                "surround_7_1_opus_bitrate": "320k",
            },
            "subtitle": {"prefer_text": True},
        }


if __name__ == "__main__":
    unittest.main()
