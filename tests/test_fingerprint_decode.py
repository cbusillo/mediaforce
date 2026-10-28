import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.encoding.fingerprint import (
    DEFAULT_FINGERPRINT_MAX_FRAMES, DEFAULT_FINGERPRINT_RANGE_COUNT, analyze_media_fingerprint,
    _run_audio_range, _run_fingerprint_range, fingerprint_video_decode_timed_out, parse_ebur128_summary,
    reclassify_media_fingerprint_summary,
)


def _frame_metadata(luma: int, count: int = 1) -> str:
    return "\n".join(
        f"frame:{index} pts:{index}\nlavfi.signalstats.YAVG={luma}\nlavfi.signalstats.YDIF=2"
        for index in range(count)
    )


def _video_result(luma: int, count: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, _frame_metadata(luma, count), _frame_metadata(51, count))


def _fingerprint(runner: Mock, *, audio_streams: list[dict[str, object]] | None = None) -> dict[str, Any]:
    runner.tool_lineage.return_value = {"ffmpeg_version": "test"}
    return analyze_media_fingerprint(
        Path("episode.mkv"), video_stream={"codec_name": "h264"}, audio_streams=audio_streams or [],
        duration_seconds=1200, command_runner=runner,
    )


def test_combined_pass_preserves_signal_and_edge_metadata() -> None:
    runner = Mock()
    frames_per_range = 120
    runner.run.return_value = _video_result(24, frames_per_range)
    summary = _fingerprint(runner)
    ranges = summary["analysis"]["ranges"]

    assert runner.run.call_count == len(ranges)
    assert summary["analysis"]["coverage"] == 1
    aggregate = summary["analysis"]["aggregate"]
    assert aggregate["sampled_frames"] == frames_per_range * len(ranges)
    assert aggregate["dark_frame_fraction"] == 1
    assert aggregate["edge_density_mean"] == 0.2
    assert aggregate["duplicate_like_frame_fraction"] == 0


_AUDIO_OUTPUT = "Integrated loudness:\n I: -18.5 LUFS\nLoudness range:\n LRA: 13.2 LU\nTrue peak:\n Peak: -1.0 dBFS\n"


def test_audio_and_video_share_each_range_command() -> None:
    video = _video_result(24, DEFAULT_FINGERPRINT_MAX_FRAMES // DEFAULT_FINGERPRINT_RANGE_COUNT)
    runner = Mock()
    runner.run.return_value = subprocess.CompletedProcess([], 0, video.stdout, video.stderr + "\n" + _AUDIO_OUTPUT)
    summary = _fingerprint(runner, audio_streams=[{"channels": 2}])

    assert runner.run.call_count == len(summary["analysis"]["ranges"])
    assert summary["analysis"]["sampled_frames"] == DEFAULT_FINGERPRINT_MAX_FRAMES
    assert all(row["audio"]["measurements"] == parse_ebur128_summary(_AUDIO_OUTPUT) for row in summary["analysis"]["ranges"])


def test_combined_failure_keeps_independent_video_and_audio_results() -> None:
    runner = Mock()
    video = _video_result(24, DEFAULT_FINGERPRINT_MAX_FRAMES // DEFAULT_FINGERPRINT_RANGE_COUNT)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "-filter_complex" in command:
            if "[audio_out]" in command[command.index("-filter_complex") + 1]:
                return subprocess.CompletedProcess(command, 1, "", "audio graph unavailable")
            return video
        return subprocess.CompletedProcess(command, 0, "", _AUDIO_OUTPUT)

    runner.run.side_effect = run
    summary = _fingerprint(runner, audio_streams=[{"channels": 2}])
    assert summary["analysis"]["sampled_frames"] == DEFAULT_FINGERPRINT_MAX_FRAMES
    assert summary["analysis"]["coverage"] == 1
    assert all(row["audio"]["measurements"] == parse_ebur128_summary(_AUDIO_OUTPUT) for row in summary["analysis"]["ranges"])


def test_failed_range_retains_independently_measured_ranges() -> None:
    runner = Mock()
    runner.run.side_effect = [
        _video_result(24, 120), subprocess.CompletedProcess([], 1, "", ""), _video_result(130, 120),
    ]
    summary = _fingerprint(runner)
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


@pytest.mark.parametrize("hardware_frames, calls", [(20, 1), (19, 2)])
def test_short_final_range_requires_expected_frames(hardware_frames: int, calls: int) -> None:
    runner = Mock()
    runner.tool_lineage.return_value = {"ffmpeg_version": "test"}
    runner.run.side_effect = [_video_result(24, hardware_frames), _video_result(24, 20)]
    summary = analyze_media_fingerprint(
        Path("short.mkv"), video_stream={"codec_name": "h264", "pix_fmt": "yuv420p"},
        audio_streams=[], duration_seconds=10, max_frames=120, range_count=1, sample_fps=2, command_runner=runner,
    )

    assert runner.run.call_count == calls
    assert summary["analysis"]["ranges"][-1]["sampled_frames"] == 20


def test_software_recovery_does_not_attempt_hardware_again() -> None:
    runner = Mock()
    runner.run.return_value = _video_result(24, 120)
    runner.tool_lineage.return_value = {"ffmpeg_version": "test"}
    summary = analyze_media_fingerprint(
        Path("episode.mkv"), video_stream={"codec_name": "h264", "pix_fmt": "yuv420p"},
        audio_streams=[], duration_seconds=1200, hardware_decoding=False, command_runner=runner,
    )

    assert all(row["decoder"] == "software" for row in summary["analysis"]["ranges"])
    assert runner.run.call_count == len(summary["analysis"]["ranges"])


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


@pytest.mark.parametrize("timeout_phase", ["combined", "audio"])
def test_timeout_does_not_start_another_decode(timeout_phase: str) -> None:
    runner = Mock()
    completed = [subprocess.CompletedProcess([], 1, "", "combined pass failed"), _video_result(24, 120)] if timeout_phase == "audio" else []
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
    assert fingerprint_video_decode_timed_out(summary) is (timeout_phase == "combined")


def test_missing_edge_metadata_cannot_claim_coverage() -> None:
    runner = Mock()
    runner.run.return_value = subprocess.CompletedProcess([], 0, _frame_metadata(24, 120), "")
    summary = _fingerprint(runner)

    assert summary["decision"]["status"] == "unknown"
    assert summary["analysis"]["coverage"] == 0


def test_failed_pass_cannot_claim_measured_coverage() -> None:
    runner = Mock()
    runner.run.return_value = subprocess.CompletedProcess([], 1, _frame_metadata(24), "decoder failed")
    summary = _fingerprint(runner)

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
    assert all(row["status"] == "measured" for row in ranges)
    assert sum(row["sampled_frames"] for row in ranges) == frame_budget
    assert summary["analysis"]["aggregate"]["edge_density_mean"] > 0


@pytest.mark.parametrize(("frame_budget", "sample_fps"), [(24, 2), (3, 1), (8, 2)])
def test_real_combined_audio_matches_independent_window(tmp_path: Path, frame_budget: int, sample_fps: int) -> None:
    binary = ffmpeg_binary()
    if shutil.which(binary) is None:
        pytest.skip("FFmpeg is required for the decode protocol check")
    source = tmp_path / "audio-pattern.mkv"
    subprocess.run(
        [binary, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=24:duration=16",
         "-f", "lavfi", "-i", "sine=frequency=637:sample_rate=48000:duration=16,volume='if(lt(t,3),0.01,1)':eval=frame",
         "-c:v", "mpeg4", "-q:v", "5", "-c:a", "pcm_s16le", str(source)],
        check=True, capture_output=True, timeout=15,
    )
    summary = analyze_media_fingerprint(
        source, video_stream={"codec_name": "mpeg4"}, audio_streams=[{"channels": 1}], duration_seconds=16,
        max_frames=frame_budget, range_count=1, sample_fps=sample_fps, timeout_seconds=5,
    )
    sample = summary["analysis"]["ranges"][0]
    reference = _run_audio_range(source, ffmpeg_name=binary, start_seconds=sample["start_seconds"],
        duration_seconds=sample["audio"]["duration_seconds"], timeout_seconds=5, process_controller=None, command_runner=None)
    assert sample["sampled_frames"] == frame_budget
    assert sample["audio"]["status"] == reference["status"] == "measured"
    assert sample["audio"]["measurements"] == parse_ebur128_summary(reference["output"])
