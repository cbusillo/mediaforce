import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mediaforce.core.evidence import build_evidence_envelope
from mediaforce.encoding.cadence import (
    CadenceResolutionError,
    analyze_cadence,
    cadence_filter,
    cadence_manifest_payload,
    classify_cadence,
    parse_idet_output,
)
from mediaforce.encoding.commands import build_ffmpeg_command
from mediaforce.encoding.quality import QualitySearchResult
from mediaforce.encoding.quality_search import search_quality
from mediaforce.encoding.video_filters import build_video_filter
from mediaforce.library.probe import probe_media


class CadenceTests(unittest.TestCase):
    def test_parse_idet_output_uses_final_aggregate_counts(self) -> None:
        stderr = """
[Parsed_idet_0] Repeated Fields: Neither:   120 Top:    20 Bottom:    10
[Parsed_idet_0] Multi frame detection: TFF:    55 BFF:     5 Progressive:    90 Undetermined:     0
[Parsed_idet_0] Repeated Fields: Neither:   240 Top:    40 Bottom:    20
[Parsed_idet_0] Multi frame detection: TFF:   110 BFF:    10 Progressive:   180 Undetermined:     0
"""

        self.assertEqual(
            parse_idet_output(stderr),
            {
                "tff_frames": 110,
                "bff_frames": 10,
                "progressive_frames": 180,
                "undetermined_frames": 0,
                "repeated_neither": 240,
                "repeated_top": 40,
                "repeated_bottom": 20,
            },
        )

    def test_progressive_probe_metadata_avoids_idet(self) -> None:
        with patch("mediaforce.encoding.cadence._run_idet_range") as run_idet:
            summary = analyze_cadence(
                Path("/tmp/progressive.mkv"),
                video_stream={
                    "field_order": "progressive",
                    "avg_frame_rate": "24000/1001",
                    "r_frame_rate": "24000/1001",
                    "time_base": "1/1000",
                },
                duration_seconds=2700.0,
            )

        run_idet.assert_not_called()
        self.assertEqual(summary["decision"]["classification"], "progressive")
        self.assertEqual(summary["decision"]["transform"], "none")

    def test_probe_summary_persists_frame_rate_and_field_order_evidence(self) -> None:
        payload = {
            "format": {"duration": "2700.0"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "pix_fmt": "yuv420p",
                    "field_order": "progressive",
                    "avg_frame_rate": "24000/1001",
                    "r_frame_rate": "24000/1001",
                    "time_base": "1/90000",
                }
            ],
        }
        with patch("mediaforce.library.probe.ffprobe_binary", return_value="ffprobe"), patch(
            "mediaforce.library.probe.subprocess.run",
            return_value=Mock(stdout=json.dumps(payload)),
        ) as run_probe:
            summary = probe_media(Path("/tmp/progressive.mkv"))

        run_probe.assert_called_once()
        cadence = json.loads(summary.cadence_summary_json)
        self.assertEqual(cadence["probe"]["field_order"], "progressive")
        self.assertEqual(cadence["probe"]["average_frame_rate"], "24000/1001")
        self.assertEqual(cadence["probe"]["time_base"], "1/90000")

    def test_probe_summary_persists_stream_bytes_and_attachments(self) -> None:
        payload = {
            "format": {"duration": "120.0"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "field_order": "progressive",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "tags": {
                        "language": "eng",
                        "BPS": "192000",
                        "NUMBER_OF_BYTES": "2880000",
                        "DURATION": "00:02:00.000000000",
                    },
                },
                {
                    "index": 2,
                    "codec_type": "attachment",
                    "codec_name": "ttf",
                    "extradata_size": 250000,
                    "tags": {"filename": "font.ttf", "mimetype": "application/x-truetype-font"},
                },
            ],
        }
        with patch("mediaforce.library.probe.ffprobe_binary", return_value="ffprobe"), patch(
            "mediaforce.library.probe.subprocess.run",
            return_value=Mock(stdout=json.dumps(payload)),
        ):
            summary = probe_media(Path("/tmp/attachments.mkv"))

        audio = json.loads(summary.audio_summary_json)[0]
        attachment = json.loads(summary.attachment_summary_json)[0]
        self.assertEqual(audio["bit_rate"], 192000)
        self.assertEqual(audio["size_bytes"], 2880000)
        self.assertEqual(audio["duration_seconds"], 120.0)
        self.assertEqual(attachment["file_name"], "font.ttf")
        self.assertEqual(attachment["mime_type"], "application/x-truetype-font")
        self.assertEqual(attachment["size_bytes"], 250000)

    def test_unknown_probe_metadata_uses_bounded_representative_ranges(self) -> None:
        stderr = (
            "Repeated Fields: Neither: 200 Top: 0 Bottom: 0\n"
            "Multi frame detection: TFF: 190 BFF: 0 Progressive: 10 Undetermined: 0\n"
        )
        with patch("mediaforce.encoding.cadence.ffmpeg_binary", return_value="ffmpeg"), patch(
            "mediaforce.encoding.cadence._ffmpeg_version",
            return_value="ffmpeg version test",
        ), patch(
            "mediaforce.encoding.cadence._run_idet_range",
            return_value={"status": "measured", "stderr": stderr},
        ) as run_range:
            summary = analyze_cadence(
                Path("/tmp/interlaced.mkv"),
                video_stream={"field_order": "unknown"},
                duration_seconds=1000.0,
                max_frames=600,
                range_count=3,
            )

        self.assertEqual(run_range.call_count, 3)
        self.assertEqual(
            [call.kwargs["start_seconds"] for call in run_range.call_args_list],
            [100.0, 500.0, 900.0],
        )
        self.assertEqual({call.kwargs["frame_limit"] for call in run_range.call_args_list}, {200})
        self.assertEqual(summary["analysis"]["sampled_frames"], 600)
        self.assertEqual(summary["decision"]["classification"], "tff")
        self.assertEqual(summary["decision"]["transform"], "bwdif_tff")

    def test_progressive_2997_probe_runs_idet_to_check_for_telecine(self) -> None:
        stderr = (
            "Repeated Fields: Neither: 200 Top: 0 Bottom: 0\n"
            "Multi frame detection: TFF: 0 BFF: 0 Progressive: 200 Undetermined: 0\n"
        )
        with patch("mediaforce.encoding.cadence.ffmpeg_binary", return_value="ffmpeg"), patch(
            "mediaforce.encoding.cadence._ffmpeg_version",
            return_value="ffmpeg version test",
        ), patch(
            "mediaforce.encoding.cadence._run_idet_range",
            return_value={"status": "measured", "stderr": stderr},
        ) as run_range:
            summary = analyze_cadence(
                Path("/tmp/progressive-2997.mkv"),
                video_stream={
                    "field_order": "progressive",
                    "avg_frame_rate": "30000/1001",
                    "r_frame_rate": "30000/1001",
                },
                duration_seconds=2700.0,
            )

        self.assertEqual(run_range.call_count, 3)
        self.assertEqual(summary["decision"]["classification"], "progressive")

    def test_progressive_2997_probe_blocks_when_required_idet_fails(self) -> None:
        with patch("mediaforce.encoding.cadence.ffmpeg_binary", return_value="ffmpeg"), patch(
            "mediaforce.encoding.cadence._ffmpeg_version",
            return_value="ffmpeg version test",
        ), patch(
            "mediaforce.encoding.cadence._run_idet_range",
            return_value={"status": "failed", "stderr": "decode failed"},
        ):
            summary = analyze_cadence(
                Path("/tmp/progressive-2997.mkv"),
                video_stream={
                    "field_order": "progressive",
                    "avg_frame_rate": "30000/1001",
                    "r_frame_rate": "30000/1001",
                },
                duration_seconds=2700.0,
            )

        self.assertEqual(summary["decision"]["classification"], "unknown")
        self.assertEqual(summary["decision"]["status"], "blocked")

    def test_partial_or_timed_out_ranges_cannot_resolve_a_transform(self) -> None:
        stderr = (
            "Repeated Fields: Neither: 200 Top: 0 Bottom: 0\n"
            "Multi frame detection: TFF: 190 BFF: 0 Progressive: 10 Undetermined: 0\n"
        )
        results = [
            {"status": "measured", "stderr": stderr},
            {"status": "timeout", "stderr": stderr},
            {"status": "failed", "stderr": stderr},
        ]
        with patch("mediaforce.encoding.cadence.ffmpeg_binary", return_value="ffmpeg"), patch(
            "mediaforce.encoding.cadence._ffmpeg_version",
            return_value="ffmpeg version test",
        ), patch(
            "mediaforce.encoding.cadence._run_idet_range",
            side_effect=results,
        ):
            summary = analyze_cadence(
                Path("/tmp/partial.mkv"),
                video_stream={"field_order": "unknown", "avg_frame_rate": "30000/1001"},
                duration_seconds=1000.0,
            )

        self.assertEqual(summary["analysis"]["sampled_frames"], 200)
        self.assertLess(summary["decision"]["coverage"], 0.5)
        self.assertEqual(summary["decision"]["status"], "blocked")
        self.assertIsNone(summary["decision"]["transform"])
        _, manifest_decision = cadence_manifest_payload(
            summary,
            source_id="src1_test",
            source_fingerprint="fingerprint-1",
        )
        self.assertEqual(manifest_decision["status"], "blocked")
        self.assertIsNone(manifest_decision["transform"])

    def test_classifier_covers_interlaced_telecine_mixed_and_unknown(self) -> None:
        tff = self._decision(tff_frames=190, progressive_frames=10)
        bff = self._decision(bff_frames=190, progressive_frames=10)
        telecine = self._decision(
            tff_frames=80,
            progressive_frames=120,
            repeated_neither=170,
            repeated_top=20,
            repeated_bottom=10,
        )
        mixed = self._decision(tff_frames=80, progressive_frames=80, undetermined_frames=40)
        unknown = self._decision(tff_frames=10, progressive_frames=10)

        self.assertEqual((tff["classification"], tff["transform"]), ("tff", "bwdif_tff"))
        self.assertEqual((bff["classification"], bff["transform"]), ("bff", "bwdif_bff"))
        self.assertEqual(
            (telecine["classification"], telecine["transform"]),
            ("telecine", "fieldmatch_decimate"),
        )
        self.assertEqual((mixed["classification"], mixed["status"]), ("mixed", "blocked"))
        self.assertEqual((unknown["classification"], unknown["status"]), ("unknown", "blocked"))

    def test_transform_compiler_rejects_unresolved_or_mismatched_plans(self) -> None:
        tff, tff_evidence = self._backed_decision(tff_frames=190, progressive_frames=10)
        self.assertEqual(
            cadence_filter(tff, tff_evidence, source_fingerprint="fingerprint-1"),
            "bwdif=mode=send_frame:parity=tff:deint=all",
        )

        mismatched = {**tff, "transform": "fieldmatch,decimate"}
        with self.assertRaisesRegex(CadenceResolutionError, "does not match"):
            cadence_filter(mismatched, tff_evidence, source_fingerprint="fingerprint-1")
        mixed, mixed_evidence = self._backed_decision(
            tff_frames=80,
            progressive_frames=80,
            undetermined_frames=40,
        )
        with self.assertRaisesRegex(CadenceResolutionError, "unresolved"):
            cadence_filter(mixed, mixed_evidence, source_fingerprint="fingerprint-1")
        with self.assertRaisesRegex(CadenceResolutionError, "not backed"):
            cadence_filter(
                {
                    "schema_version": 1,
                    "classification": "tff",
                    "status": "resolved",
                    "confidence": 1.0,
                    "coverage": 1.0,
                    "high_risk": True,
                    "transform": "bwdif_tff",
                    "rationale": "unbacked",
                },
                None,
                source_fingerprint="fingerprint-1",
            )

    def test_significant_opposite_parity_is_mixed_not_dominant_interlace(self) -> None:
        decision = self._decision(tff_frames=170, bff_frames=30)

        self.assertEqual(decision["classification"], "mixed")
        self.assertEqual(decision["status"], "blocked")

    def test_one_sided_repeated_fields_do_not_masquerade_as_telecine(self) -> None:
        decision = self._decision(
            tff_frames=200,
            repeated_neither=170,
            repeated_top=30,
        )

        self.assertEqual(decision["classification"], "tff")
        self.assertEqual(decision["transform"], "bwdif_tff")

    def test_transform_gate_rejects_stale_source_evidence(self) -> None:
        decision, evidence = self._backed_decision(tff_frames=190, progressive_frames=10)

        with self.assertRaisesRegex(CadenceResolutionError, "source fingerprint"):
            cadence_filter(
                decision,
                evidence,
                source_fingerprint="fingerprint-2",
            )

    def test_transform_gate_rejects_stale_transform_policy_evidence(self) -> None:
        decision, evidence = self._backed_decision(tff_frames=190, progressive_frames=10)
        inputs = evidence["inputs"]
        tool = inputs["tool"]
        stale_evidence = build_evidence_envelope(
            kind=str(evidence["kind"]),
            sources=inputs["sources"],
            policy_hash="sha256:stale-transform-policy",
            tool_name=str(tool["name"]),
            tool_version=str(tool["version"]),
            subject=evidence["subject"],
            measurement=evidence["measurement"],
            result=evidence["result"],
        )
        stale_decision = {**decision, "evidence_id": stale_evidence["evidence_id"]}

        with self.assertRaisesRegex(CadenceResolutionError, "transform policy"):
            cadence_filter(
                stale_decision,
                stale_evidence,
                source_fingerprint="fingerprint-1",
            )

    def test_names_years_and_categories_do_not_select_a_transform(self) -> None:
        analysis = self._analysis(tff_frames=20, progressive_frames=20)
        decision = classify_cadence(
            probe={
                "field_order": "unknown",
                "average_frame_rate": "30000/1001",
                "file_name": "Classic.NTSC.Telecine.1987.mkv",
                "release_year": 1987,
                "category": "old television",
            },
            analysis=analysis,
            coverage=1.0,
        )

        self.assertEqual(decision["classification"], "unknown")
        self.assertEqual(decision["status"], "blocked")
        self.assertIsNone(decision["transform"])

    def test_manifest_evidence_is_stable_and_carries_transform_identity(self) -> None:
        decision = self._decision(tff_frames=190, progressive_frames=10)
        summary = {
            "schema_version": 1,
            "probe": {
                "field_order": "tff",
                "average_frame_rate": "30000/1001",
                "nominal_frame_rate": "30000/1001",
                "time_base": "1/90000",
            },
            "analysis": {
                **self._analysis(tff_frames=190, progressive_frames=10),
                "ranges": [
                    {
                        "start_seconds": 120.0,
                        "frame_limit": 200,
                        "sampled_frames": 200,
                        "status": "measured",
                    }
                ],
                "tool": {
                    "name": "mediaforce.ffmpeg_idet",
                    "version": "1",
                    "ffmpeg_version": "ffmpeg version test",
                },
            },
            "decision": decision,
        }

        first = cadence_manifest_payload(
            summary,
            source_id="src1_test",
            source_fingerprint="fingerprint-1",
        )
        second = cadence_manifest_payload(
            summary,
            source_id="src1_test",
            source_fingerprint="fingerprint-1",
        )

        self.assertEqual(first, second)
        evidence, resolved = first
        assert evidence is not None
        self.assertEqual(resolved["evidence_id"], evidence["evidence_id"])
        self.assertEqual(resolved["transform"], "bwdif_tff")
        self.assertEqual(evidence["measurement"]["media_ranges"][0]["start_seconds"], 120.0)

    def test_manifest_recomputes_decision_instead_of_trusting_stored_claim(self) -> None:
        summary = {
            "schema_version": 1,
            "probe": {"field_order": "unknown", "average_frame_rate": "30000/1001"},
            "analysis": {
                **self._analysis(tff_frames=10, progressive_frames=10),
                "ranges": [],
                "tool": {"name": "mediaforce.ffmpeg_idet", "version": "1"},
            },
            "decision": {
                "schema_version": 1,
                "classification": "tff",
                "status": "resolved",
                "confidence": 1.0,
                "coverage": 1.0,
                "high_risk": True,
                "transform": "bwdif_tff",
                "rationale": "fabricated",
            },
        }

        evidence, decision = cadence_manifest_payload(
            summary,
            source_id="src1_test",
            source_fingerprint="fingerprint-1",
        )

        assert evidence is not None
        self.assertEqual(decision["classification"], "unknown")
        self.assertEqual(decision["status"], "blocked")
        with self.assertRaises(CadenceResolutionError):
            cadence_filter(decision, evidence, source_fingerprint="fingerprint-1")

    def test_manifest_blocks_evidence_from_an_old_analyzer_version(self) -> None:
        summary = {
            "schema_version": 1,
            "probe": {"field_order": "unknown", "average_frame_rate": "30000/1001"},
            "analysis": {
                **self._analysis(tff_frames=190, progressive_frames=10),
                "ranges": [],
                "tool": {
                    "name": "mediaforce.ffmpeg_idet",
                    "version": "0",
                    "ffmpeg_version": "ffmpeg version test",
                },
            },
            "decision": self._decision(tff_frames=190, progressive_frames=10),
        }

        evidence, decision = cadence_manifest_payload(
            summary,
            source_id="src1_test",
            source_fingerprint="fingerprint-1",
        )

        assert evidence is not None
        self.assertEqual(decision["classification"], "unknown")
        self.assertIn("unsupported analyzer version", str(decision["rationale"]))

    def test_sample_and_production_compile_the_same_filter(self) -> None:
        video_policy = {
            "encoder": "libsvtav1",
            "pixel_format": "yuv420p10le",
            "quality_metric": "vmaf",
            "target_vmaf": 85.0,
            "min_target_vmaf": 80.0,
            "target_relax_step_vmaf": 0.5,
            "sample_every": "8m",
            "sample_duration": "20s",
            "min_crf": 18,
            "max_crf": 38,
            "max_encoded_percent": 80,
            "max_height": 720,
        }
        decision, evidence = self._backed_decision(
            tff_frames=190,
            progressive_frames=10,
        )
        run_search = Mock(
            return_value=QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=86.0,
                stdout="ok",
            )
        )
        search_quality(
            Path("/tmp/input.mkv"),
            video_policy,
            width=1920,
            height=1080,
            cadence_decision=decision,
            cadence_evidence=evidence,
            cadence_source_fingerprint="fingerprint-1",
            host_media_access_for_host=Mock(return_value="mounted"),
            select_quality_metric=Mock(return_value=("vmaf", 85.0)),
            build_svt_params=Mock(return_value=[]),
            effective_video_preset=Mock(return_value=4),
            run_crf_search=run_search,
        )
        sample_filter = run_search.call_args.kwargs["video_filter"]

        command = build_ffmpeg_command(
            source_path=Path("/tmp/input.mkv"),
            staging_path=Path("/tmp/output.mkv"),
            source_codec="h264",
            video_policy=video_policy,
            preset=4,
            audio_policy={},
            subtitle_policy={},
            selection={"audio_tracks": [], "subtitle_tracks": []},
            quality=run_search.return_value,
            cadence_decision=decision,
            cadence_evidence=evidence,
            cadence_source_fingerprint="fingerprint-1",
            width=1920,
            height=1080,
            text_subtitle_codecs=set(),
            ffmpeg_binary=lambda: "ffmpeg",
            ffmpeg_hwaccel_input_args=lambda *_args, **_kwargs: [],
            audio_codec=lambda *_args: "copy",
            opus_layout_filter=lambda *_args: None,
            opus_bitrate=lambda *_args: "128k",
            format_crf=lambda value: str(value),
        )
        production_filter = command[command.index("-vf") + 1]

        self.assertEqual(sample_filter, production_filter)
        self.assertEqual(
            sample_filter,
            build_video_filter(
                video_policy,
                width=1920,
                height=1080,
                cadence_decision=decision,
                cadence_evidence=evidence,
                cadence_source_fingerprint="fingerprint-1",
            ),
        )

    def test_quality_search_blocks_before_running_for_mixed_cadence(self) -> None:
        run_search = Mock()
        video_policy = {
            "quality_metric": "vmaf",
            "target_vmaf": 85.0,
            "min_target_vmaf": 80.0,
            "target_relax_step_vmaf": 0.5,
            "pixel_format": "yuv420p10le",
            "sample_every": "8m",
            "sample_duration": "20s",
            "min_crf": 18,
            "max_crf": 38,
            "max_encoded_percent": 80,
        }
        decision, evidence = self._backed_decision(
            tff_frames=80,
            progressive_frames=80,
            undetermined_frames=40,
        )
        with self.assertRaises(CadenceResolutionError):
            search_quality(
                Path("/tmp/input.mkv"),
                video_policy,
                cadence_decision=decision,
                cadence_evidence=evidence,
                cadence_source_fingerprint="fingerprint-1",
                host_media_access_for_host=Mock(return_value="mounted"),
                select_quality_metric=Mock(return_value=("vmaf", 85.0)),
                build_svt_params=Mock(return_value=[]),
                effective_video_preset=Mock(return_value=4),
                run_crf_search=run_search,
            )

        run_search.assert_not_called()

    @staticmethod
    def _analysis(**overrides: int) -> dict[str, int]:
        values = {
            "tff_frames": 0,
            "bff_frames": 0,
            "progressive_frames": 0,
            "undetermined_frames": 0,
            "repeated_neither": 0,
            "repeated_top": 0,
            "repeated_bottom": 0,
        }
        values.update(overrides)
        values["sampled_frames"] = sum(
            values[key]
            for key in ("tff_frames", "bff_frames", "progressive_frames", "undetermined_frames")
        )
        return values

    @classmethod
    def _decision(cls, **overrides: int) -> dict[str, object]:
        analysis = cls._analysis(**overrides)
        return classify_cadence(
            probe={"field_order": "unknown", "average_frame_rate": "30000/1001"},
            analysis=analysis,
            coverage=1.0,
        )

    @classmethod
    def _backed_decision(
            cls,
            **overrides: int,
    ) -> tuple[dict[str, object], dict[str, object]]:
        analysis = cls._analysis(**overrides)
        summary = {
            "schema_version": 1,
            "probe": {"field_order": "unknown", "average_frame_rate": "30000/1001"},
            "analysis": {
                **analysis,
                "ranges": [
                    {
                        "start_seconds": 0.0,
                        "frame_limit": 200,
                        "sampled_frames": analysis["sampled_frames"],
                        "status": "measured",
                    }
                ],
                "tool": {
                    "name": "mediaforce.ffmpeg_idet",
                    "version": "1",
                    "ffmpeg_version": "ffmpeg version test",
                },
            },
            "decision": cls._decision(**overrides),
        }
        evidence, decision = cadence_manifest_payload(
            summary,
            source_id="src1_test",
            source_fingerprint="fingerprint-1",
        )
        assert evidence is not None
        return decision, evidence


if __name__ == "__main__":
    unittest.main()
