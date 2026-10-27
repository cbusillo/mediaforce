import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.encoding.fingerprint import analyze_media_fingerprint, _run_fingerprint_range, reclassify_media_fingerprint_summary


def _frame_metadata(luma: int, count: int = 1) -> str:
    return "\n".join(
        f"frame:{index} pts:{index}\nlavfi.signalstats.YAVG={luma}\nlavfi.signalstats.YDIF=2"
        for index in range(count)
    )


def _video_result(luma: int, count: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, _frame_metadata(luma, count), _frame_metadata(51, count))


def _fingerprint_without_audio(runner: Mock) -> dict[str, Any]:
    runner.tool_lineage.return_value = {"ffmpeg_version": "test"}
    return analyze_media_fingerprint(
        Path("episode.mkv"), video_stream={"codec_name": "h264"}, audio_streams=[],
        duration_seconds=1200, command_runner=runner,
    )


def test_combined_pass_preserves_signal_and_edge_metadata() -> None:
    runner = Mock()
    frames_per_range = 120
    runner.run.return_value = _video_result(24, frames_per_range)
    summary = _fingerprint_without_audio(runner)
    ranges = summary["analysis"]["ranges"]

    assert runner.run.call_count == len(ranges)
    assert summary["analysis"]["coverage"] == 1
    aggregate = summary["analysis"]["aggregate"]
    assert aggregate["sampled_frames"] == frames_per_range * len(ranges)
    assert aggregate["dark_frame_fraction"] == 1
    assert aggregate["edge_density_mean"] == 0.2
    assert aggregate["duplicate_like_frame_fraction"] == 0


def test_failed_range_retains_independently_measured_ranges() -> None:
    runner = Mock()
    runner.run.side_effect = [
        _video_result(24, 120), subprocess.CompletedProcess([], 1, "", ""), _video_result(130, 120),
    ]
    summary = _fingerprint_without_audio(runner)
    ranges = summary["analysis"]["ranges"]

    assert [row["status"] for row in ranges] == ["measured", "failed", "measured"]
    assert summary["analysis"]["measured_range_count"] == 2
    assert summary["analysis"]["coverage"] < 1
    assert ranges[0]["measurements"]["luma_mean"] == 24
    assert ranges[2]["measurements"]["luma_mean"] == 130


def test_unavailable_hardware_retries_complete_range_in_software() -> None:
    runner = Mock()
    runner.run.side_effect = [
        subprocess.CompletedProcess([], 1, "partial hardware metadata", "unsupported decoder"), _video_result(24, 2),
    ]
    result = _run_fingerprint_range(
        Path("episode.mkv"), ffmpeg_name="ffmpeg", start_seconds=120,
        frame_limit=2, sample_fps=2, download_format="nv12", timeout_seconds=60,
        process_controller=None, command_runner=runner,
    )

    assert runner.run.call_count == 2
    assert result.status == "measured"
    assert result.decoder == "software"
    assert len(result.signal_frames) == len(result.edge_frames) == 2
    first, second = [call.args[0] for call in runner.run.call_args_list]
    assert "-hwaccel" in first
    assert "-hwaccel" not in second


def test_incomplete_hardware_metadata_retries_in_software() -> None:
    runner = Mock()
    runner.run.side_effect = [_video_result(0, 1), _video_result(24, 2)]
    result = _run_fingerprint_range(
        Path("episode.mkv"), ffmpeg_name="ffmpeg", start_seconds=120, frame_limit=2,
        sample_fps=2, download_format="nv12", timeout_seconds=60,
        process_controller=None, command_runner=runner,
    )

    assert runner.run.call_count == 2
    assert result.decoder == "software"
    assert [row["yavg"] for row in result.signal_frames] == [24, 24]


def test_unmapped_pixel_format_uses_full_software_pass() -> None:
    runner = Mock()
    runner.run.return_value = _video_result(24, 120)
    runner.tool_lineage.return_value = {"ffmpeg_version": "test"}
    analyze_media_fingerprint(
        Path("episode.mkv"), video_stream={"codec_name": "hevc", "pix_fmt": "yuv444p12le"},
        audio_streams=[], duration_seconds=1200, command_runner=runner,
    )

    for call in runner.run.call_args_list:
        assert "-hwaccel" not in call.args[0]


@pytest.mark.parametrize("timeout_phase", ["video", "audio"])
def test_timeout_does_not_start_another_decode(timeout_phase: str) -> None:
    runner = Mock()
    completed = [_video_result(24, 120)] if timeout_phase == "audio" else []
    runner.run.side_effect = [*completed, subprocess.TimeoutExpired([], 1)]
    runner.tool_lineage.return_value = {"ffmpeg_version": "test"}
    summary = analyze_media_fingerprint(
        Path("episode.mkv"), video_stream={"codec_name": "h264", "pix_fmt": "yuv420p"},
        audio_streams=[{"channels": 2}], duration_seconds=1200, timeout_seconds=1, command_runner=runner,
    )

    assert runner.run.call_count == len(completed) + 1
    assert summary["analysis"]["ranges"][-1]["status"] == "timeout"
    assert summary["analysis"]["coverage"] < 1
    assert summary["decision"]["status"] == "unknown"
    assert reclassify_media_fingerprint_summary(summary)["decision"]["status"] == "unknown"


def test_missing_edge_metadata_cannot_claim_coverage() -> None:
    runner = Mock()
    runner.run.return_value = subprocess.CompletedProcess([], 0, _frame_metadata(24, 120), "")
    summary = _fingerprint_without_audio(runner)

    assert summary["decision"]["status"] == "unknown"
    assert summary["analysis"]["coverage"] == 0


def test_failed_pass_cannot_claim_measured_coverage() -> None:
    runner = Mock()
    runner.run.return_value = subprocess.CompletedProcess([], 1, _frame_metadata(24), "decoder failed")
    summary = _fingerprint_without_audio(runner)

    assert summary["analysis"]["coverage"] == 0
    assert summary["decision"]["status"] == "unknown"
    assert all(row["status"] == "failed" for row in summary["analysis"]["ranges"])


def test_real_ffmpeg_emits_bounded_signal_and_edge_frames(tmp_path: Path) -> None:
    binary = ffmpeg_binary()
    if shutil.which(binary) is None:
        pytest.skip("FFmpeg is required for the decode protocol check")
    source = tmp_path / "pattern.mkv"
    subprocess.run(
        [binary, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=24:duration=40",
         "-c:v", "mpeg4", "-q:v", "5", str(source)],
        check=True, capture_output=True, timeout=15,
    )
    frame_budget = 18
    range_count = 3
    summary = analyze_media_fingerprint(
        source, video_stream={"codec_name": "mpeg4"}, audio_streams=[], duration_seconds=40,
        max_frames=frame_budget, range_count=range_count, timeout_seconds=5,
    )
    ranges = summary["analysis"]["ranges"]

    assert len(ranges) == range_count
    assert all(row["status"] == row["edge_status"] == "measured" for row in ranges)
    assert sum(row["sampled_frames"] for row in ranges) == frame_budget
    assert summary["analysis"]["aggregate"]["edge_density_mean"] > 0
