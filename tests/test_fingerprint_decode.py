import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.encoding.fingerprint import analyze_media_fingerprint, _run_fingerprint_ranges


def test_combined_fingerprint_preserves_signal_and_edge_metadata() -> None:
    signal = "\n".join(
        f"frame:{index} pts:{index} pts_time:{index / 2}\nmediaforce.range={group}\nlavfi.signalstats.YAVG=24\n"
        "lavfi.signalstats.YDIF=2\nlavfi.signalstats.YMIN=20\nlavfi.signalstats.YMAX=32"
        for group in range(3) for index in range(120)
    )
    edge = "\n".join(
        f"frame:{index} pts:{index} pts_time:{index / 2}\nmediaforce.range={group}\nlavfi.signalstats.YAVG=51"
        for group in range(3) for index in range(120)
    )
    runner = Mock()
    runner.run.return_value = subprocess.CompletedProcess([], 0, signal, edge)
    runner.tool_lineage.return_value = {"ffmpeg_version": "test"}
    summary = _fingerprint_without_audio(runner)

    assert runner.run.call_count == 1
    assert summary["analysis"]["coverage"] == 1
    aggregate = summary["analysis"]["aggregate"]
    assert aggregate["sampled_frames"] == signal.count("frame:")
    assert aggregate["dark_frame_fraction"] == 1
    assert aggregate["edge_density_mean"] == 0.2
    assert aggregate["duplicate_like_frame_fraction"] == 0


def test_failed_combined_pass_cannot_claim_measured_coverage() -> None:
    runner = Mock()
    runner.run.return_value = subprocess.CompletedProcess(
        [], 1, "frame:0 pts:0 pts_time:0\nlavfi.signalstats.YAVG=24", "decoder failed",
    )
    runner.tool_lineage.return_value = {"ffmpeg_version": "test"}
    summary = _fingerprint_without_audio(runner)

    assert summary["analysis"]["coverage"] == 0
    assert summary["decision"]["status"] == "unknown"
    assert all(row["status"] == "failed" for row in summary["analysis"]["ranges"])


def test_unavailable_hardware_retries_complete_range_in_software() -> None:
    from mediaforce.encoding.fingerprint import _run_fingerprint_ranges

    runner = Mock()
    runner.run.side_effect = [
        subprocess.CompletedProcess([], 1, "partial hardware metadata", "unsupported decoder"),
        subprocess.CompletedProcess([], 0, _frame_metadata(0, 24), _frame_metadata(0, 51)),
    ]
    result = _run_fingerprint_ranges(
        Path("episode.mkv"), ffmpeg_name="ffmpeg", starts=[120],
        frame_limit=120, sample_fps=2, download_format="nv12", timeout_seconds=60,
        process_controller=None, command_runner=runner,
    )

    assert runner.run.call_count == 2
    assert result[0].status == "measured"
    assert result[0].signal_frames == [{"yavg": 24}]
    assert result[0].edge_frames == [{"yavg": 51}]
    first, second = [call.args[0] for call in runner.run.call_args_list]
    assert "-hwaccel" in first
    assert "-hwaccel" not in second


def test_unmapped_pixel_format_uses_full_software_pass() -> None:
    runner = Mock()
    runner.run.return_value = subprocess.CompletedProcess([], 0, "", "")
    runner.tool_lineage.return_value = {"ffmpeg_version": "test"}
    analyze_media_fingerprint(
        Path("episode.mkv"), video_stream={"codec_name": "hevc", "pix_fmt": "yuv444p12le"},
        audio_streams=[], duration_seconds=1200, command_runner=runner,
    )
    for call in runner.run.call_args_list:
        assert "-hwaccel" not in call.args[0]


def test_interleaved_fingerprint_frames_keep_range_boundaries() -> None:
    from mediaforce.encoding.fingerprint import parse_fingerprint_ranges

    output = """
frame:0 pts:0
lavfi.signalstats.YAVG=24
lavfi.signalstats.YDIF=0
mediaforce.range=2
frame:1 pts:1
lavfi.signalstats.YAVG=130
lavfi.signalstats.YDIF=0
mediaforce.range=0
frame:2 pts:2
lavfi.signalstats.YAVG=32
lavfi.signalstats.YDIF=8
mediaforce.range=2
"""
    ranges = parse_fingerprint_ranges(output)

    assert [frame["yavg"] for frame in ranges[2]] == [24, 32]
    assert [frame["yavg"] for frame in ranges[0]] == [130]
    assert 1 not in ranges


def test_missing_fingerprint_range_or_edges_cannot_claim_full_coverage() -> None:
    frames = "\n".join(
        f"frame:{index} pts:{index}\nmediaforce.range={group}\nlavfi.signalstats.YAVG=24"
        for group in range(2) for index in range(120)
    )
    runner = Mock()
    runner.run.return_value = subprocess.CompletedProcess([], 0, frames, frames)
    runner.tool_lineage.return_value = {"ffmpeg_version": "test"}
    summary = _fingerprint_without_audio(runner)
    assert summary["analysis"]["measured_range_count"] == 2
    assert summary["analysis"]["coverage"] < 1
    assert summary["analysis"]["ranges"][2]["status"] == "failed"

    runner.run.return_value = subprocess.CompletedProcess([], 0, frames, "")
    summary = _fingerprint_without_audio(runner)
    assert summary["decision"]["status"] == "unknown"
    assert summary["analysis"]["coverage"] == 0


def _fingerprint_without_audio(runner: Mock) -> dict[str, Any]:
    return analyze_media_fingerprint(
        Path("episode.mkv"), video_stream={"codec_name": "h264"}, audio_streams=[],
        duration_seconds=1200, command_runner=runner,
    )


def _frame_metadata(group: int, luma: int) -> str:
    return f"frame:0 pts:0\nmediaforce.range={group}\nlavfi.signalstats.YAVG={luma}\n"


def test_hardware_timeout_does_not_start_another_decode() -> None:
    runner = Mock()
    runner.run.side_effect = subprocess.TimeoutExpired([], 1)
    results = _run_fingerprint_ranges(
        Path("episode.mkv"), ffmpeg_name="ffmpeg", starts=[120], frame_limit=1,
        sample_fps=2, download_format="nv12", timeout_seconds=1,
        process_controller=None, command_runner=runner,
    )
    assert runner.run.call_count == 1
    assert results[0].status == "timeout"


def test_incomplete_hardware_metadata_retries_all_ranges_in_software() -> None:
    runner = Mock()
    frames = "".join(_frame_metadata(group, group * 20) for group in range(3))
    runner.run.side_effect = [
        subprocess.CompletedProcess([], 0, _frame_metadata(0, 0), _frame_metadata(0, 0)),
        subprocess.CompletedProcess([], 0, frames, frames),
    ]
    results = _run_fingerprint_ranges(
        Path("episode.mkv"), ffmpeg_name="ffmpeg", starts=[120, 600, 1080], frame_limit=1,
        sample_fps=2, download_format="nv12", timeout_seconds=60,
        process_controller=None, command_runner=runner,
    )
    assert runner.run.call_count == 2
    assert [row.signal_frames[0]["yavg"] for row in results] == [0, 20, 40]


def test_failed_combined_decode_retains_independently_measured_ranges() -> None:
    runner = Mock()
    runner.run.side_effect = [
        subprocess.CompletedProcess([], 1, "", ""),
        subprocess.CompletedProcess([], 0, _frame_metadata(0, 24), _frame_metadata(0, 51)),
        subprocess.CompletedProcess([], 1, "", ""),
        subprocess.CompletedProcess([], 0, _frame_metadata(0, 130), _frame_metadata(0, 51)),
    ]
    results = _run_fingerprint_ranges(
        Path("episode.mkv"), ffmpeg_name="ffmpeg", starts=[120, 600, 1080], frame_limit=1,
        sample_fps=2, download_format=None, timeout_seconds=60,
        process_controller=None, command_runner=runner,
    )
    assert [row.status for row in results] == ["measured", "failed", "measured"]
    assert results[0].signal_frames[0]["yavg"] == 24
    assert results[2].signal_frames[0]["yavg"] == 130


def test_real_ffmpeg_emits_bounded_tagged_signal_and_edge_frames(tmp_path: Path) -> None:
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
        source, video_stream={"codec_name": "mpeg4"}, audio_streams=[],
        duration_seconds=40, max_frames=frame_budget, range_count=range_count, timeout_seconds=5,
    )
    ranges = summary["analysis"]["ranges"]
    assert len(ranges) == range_count
    assert all(row["status"] == row["edge_status"] == "measured" for row in ranges)
    assert sum(row["sampled_frames"] for row in ranges) == frame_budget
    assert summary["analysis"]["aggregate"]["edge_density_mean"] > 0
