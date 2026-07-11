import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mediaforce.encoding.fingerprint import (
    classify_media_fingerprint,
    media_fingerprint_manifest_payload,
    media_fingerprint_staleness,
    parse_ebur128_summary,
    parse_signalstats_metadata,
)
from mediaforce.reviewing.helpers import fingerprint_review_moments, review_moment_payload


class MediaFingerprintTests(unittest.TestCase):
    def test_signalstats_parser_groups_frame_metadata(self) -> None:
        output = """
frame:0 pts:0 pts_time:0
lavfi.signalstats.YAVG=24.5
lavfi.signalstats.YDIF=1.1
lavfi.signalstats.YMIN=20
lavfi.signalstats.YMAX=32
frame:1 pts:1 pts_time:0.5
lavfi.signalstats.YAVG=130.0
lavfi.signalstats.YDIF=16.5
lavfi.signalstats.UAVG=129
lavfi.signalstats.VAVG=126
"""

        frames = parse_signalstats_metadata(output)

        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0]["yavg"], 24.5)
        self.assertEqual(frames[1]["ydif"], 16.5)

    def test_audio_complexity_parser_reads_ebur128_summary(self) -> None:
        output = """
Integrated loudness:
    I:         -18.5 LUFS
Loudness range:
    LRA:        13.2 LU
True peak:
    Peak:       -1.0 dBFS
"""

        self.assertEqual(
            parse_ebur128_summary(output),
            {
                "integrated_loudness_lufs": -18.5,
                "loudness_range_lu": 13.2,
                "true_peak_db": -1.0,
            },
        )

    def test_dark_gradient_motion_texture_noise_and_audio_findings(self) -> None:
        decision = classify_media_fingerprint(
            self._analysis(
                dark_frame_fraction=0.62,
                gradient_frame_fraction=0.38,
                high_motion_frame_fraction=0.30,
                ydif_p90=18.0,
                high_texture_frame_fraction=0.35,
                edge_density_p90=0.12,
                temporal_noise_proxy=3.5,
                duplicate_like_frame_fraction=0.10,
                audio_loudness_range_lu_max=14.0,
                audio_probe={"track_count": 2, "max_channels": 6, "max_bitrate_bps": 900000},
            )
        )

        traits = set(decision["traits"])
        self.assertIn("dark_luma", traits)
        self.assertIn("dark_gradient_banding_risk", traits)
        self.assertIn("high_motion", traits)
        self.assertIn("high_texture", traits)
        self.assertIn("likely_film_grain", traits)
        self.assertIn("audio_complexity", traits)

    def test_clean_animation_cues_do_not_become_grain_cleanup(self) -> None:
        decision = classify_media_fingerprint(
            self._analysis(
                duplicate_like_frame_fraction=0.55,
                edge_density_p90=0.055,
                high_motion_frame_fraction=0.05,
                temporal_noise_proxy=0.6,
            )
        )

        traits = set(decision["traits"])
        self.assertIn("duplicate_cadence", traits)
        self.assertIn("animation_cues", traits)
        self.assertNotIn("likely_film_grain", traits)
        self.assertEqual(decision["policy_gates"]["automatic_denoise"], "not_allowed_from_fingerprint_alone")

    def test_unknown_and_low_coverage_findings_are_advisory_only(self) -> None:
        unknown = classify_media_fingerprint(self._analysis(sampled_frames=12, coverage=0.13))

        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(unknown["traits"], ["unknown"])

        low_confidence = classify_media_fingerprint(
            self._analysis(
                coverage=0.45,
                temporal_noise_proxy=2.4,
                chroma_instability=1.6,
                edge_density_p90=0.03,
            )
        )
        self.assertIn("likely_analog_noise", low_confidence["traits"])
        self.assertIn("likely_analog_noise", low_confidence["low_confidence_advisories"])
        self.assertEqual(low_confidence["policy_gates"]["low_confidence_findings"], "advisory_only")

    def test_compression_noise_advisory_is_low_confidence_and_non_destructive(self) -> None:
        decision = classify_media_fingerprint(
            self._analysis(
                temporal_noise_proxy=1.4,
                edge_density_p90=0.02,
                smooth_temporal_noise_fraction=0.32,
            )
        )

        self.assertIn("compression_noise_advisory", decision["traits"])
        self.assertIn("compression_noise_advisory", decision["low_confidence_advisories"])
        self.assertEqual(decision["policy_gates"]["automatic_denoise"], "not_allowed_from_fingerprint_alone")

    def test_manifest_evidence_is_source_fingerprint_scoped_and_detects_staleness(self) -> None:
        summary = {"schema_version": 1, "analysis": self._analysis(), "decision": {}}

        evidence, decision = media_fingerprint_manifest_payload(
            summary,
            source_id="src1_test",
            source_fingerprint="fingerprint-1",
        )

        assert evidence is not None
        self.assertEqual(decision["evidence_id"], evidence["evidence_id"])
        self.assertEqual(evidence["kind"], "media_fingerprint")
        self.assertEqual(evidence["inputs"]["sources"][0]["fingerprint"], "fingerprint-1")
        self.assertEqual(
            media_fingerprint_staleness(
                evidence,
                source_id="src1_test",
                source_fingerprint="fingerprint-2",
                tool_version=evidence["inputs"]["tool"]["version"],
            )["reasons"],
            ["source_fingerprint_changed"],
        )
        self.assertEqual(
            media_fingerprint_staleness(
                evidence,
                source_id="src1_test",
                source_fingerprint="fingerprint-1",
                tool_version="2|ffmpeg version changed",
            )["reasons"],
            ["tool_changed"],
        )

    def test_path_name_does_not_determine_grain_policy(self) -> None:
        dark_old_anime_path = "/media/1970s/anime/grainy_dark_episode.mkv"
        clean_modern_path = "/media/new/clean_episode.mkv"
        decision_a = classify_media_fingerprint(self._analysis(temporal_noise_proxy=0.4, edge_density_p90=0.02))
        decision_b = classify_media_fingerprint(self._analysis(temporal_noise_proxy=0.4, edge_density_p90=0.02))

        self.assertIn("grainy_dark", dark_old_anime_path)
        self.assertIn("clean", clean_modern_path)
        self.assertEqual(decision_a["traits"], decision_b["traits"])
        self.assertEqual(decision_a["policy_gates"], decision_b["policy_gates"])

    def test_review_moments_include_typical_and_hard_rationale(self) -> None:
        summary = {
            "analysis": {
                "ranges": [
                    self._range(12.0, dark_frame_fraction=0.02, ydif_p90=3.0, edge_density_p90=0.03),
                    self._range(
                        60.0,
                        dark_frame_fraction=0.80,
                        gradient_frame_fraction=0.40,
                        ydif_p90=18.0,
                        edge_density_p90=0.11,
                    ),
                ]
            }
        }
        decision = {"status": "measured", "evidence_id": "ev1_test"}

        moments = fingerprint_review_moments(
            120.0,
            8.0,
            media_fingerprint=summary,
            media_fingerprint_decision=decision,
        )

        payloads = [review_moment_payload(moment) for moment in moments]
        self.assertEqual({payload["role"] for payload in payloads}, {"typical", "hard"})
        hard = next(payload for payload in payloads if payload["role"] == "hard")
        self.assertIn("dark_gradient_banding_risk", hard["risk_tags"])
        self.assertIn("Selected because measured review risks", hard["rationale"])
        self.assertEqual(hard["evidence_id"], "ev1_test")

    def test_bounded_range_timeout_records_unknown_failure(self) -> None:
        from mediaforce.encoding import fingerprint

        with patch("mediaforce.encoding.fingerprint.subprocess.Popen") as popen:
            process = Mock()
            process.pid = 1000
            process.communicate.side_effect = [subprocess.TimeoutExpired(["ffmpeg"], 1.0), ("", "late")]
            process.poll.return_value = None
            popen.return_value = process
            with patch("mediaforce.encoding.fingerprint.os.getpgid", return_value=1000), patch(
                "mediaforce.encoding.fingerprint.os.killpg"
            ):
                result = fingerprint._run_bounded_media_command(
                    ["ffmpeg"],
                    timeout_seconds=1.0,
                    process_controller=None,
                )

        self.assertEqual(result["status"], "timeout")
        self.assertIn("Timed out", result["failure_reason"])

    @staticmethod
    def _analysis(
            *,
            sampled_frames: int = 120,
            coverage: float = 1.0,
            audio_probe: dict[str, object] | None = None,
            **metrics: float,
    ) -> dict[str, object]:
        aggregate = {
            "dark_frame_fraction": 0.05,
            "gradient_frame_fraction": 0.02,
            "banding_risk_score": 0.02,
            "high_motion_frame_fraction": 0.04,
            "ydif_p90": 3.0,
            "high_texture_frame_fraction": 0.04,
            "edge_density_p90": 0.035,
            "duplicate_like_frame_fraction": 0.05,
            "temporal_noise_proxy": 0.5,
            "chroma_instability": 0.2,
            "smooth_temporal_noise_fraction": 0.02,
            "audio_loudness_range_lu_max": 0.0,
        }
        aggregate.update(metrics)
        return {
            "sampled_frames": sampled_frames,
            "coverage": coverage,
            "aggregate": aggregate,
            "audio_probe": audio_probe or {"track_count": 0, "max_channels": 0},
            "ranges": [MediaFingerprintTests._range(10.0, **aggregate)],
            "tool": {
                "name": "mediaforce.media_fingerprint",
                "version": "1",
                "ffmpeg_version": "ffmpeg test",
            },
        }

    @staticmethod
    def _range(start_seconds: float, **metrics: float) -> dict[str, object]:
        defaults = {
            "luma_mean": 100.0,
            "dark_frame_fraction": 0.05,
            "gradient_frame_fraction": 0.02,
            "ydif_p50": 3.0,
            "ydif_p90": 3.0,
            "high_motion_frame_fraction": 0.04,
            "edge_density_mean": 0.035,
            "edge_density_p90": 0.035,
            "high_texture_frame_fraction": 0.04,
            "duplicate_like_frame_fraction": 0.05,
            "temporal_noise_proxy": 0.5,
        }
        defaults.update(metrics)
        return {
            "start_seconds": start_seconds,
            "end_seconds": start_seconds + 8.0,
            "frame_limit": 120,
            "sampled_frames": 120,
            "status": "measured",
            "measurements": defaults,
        }


if __name__ == "__main__":
    unittest.main()
