import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from mediaforce.core.config import DEFAULT_CONFIG_PATH
from mediaforce.encoding.remote_media import RemoteMediaCommands
from mediaforce.library import animation_probe as probe
from mediaforce.library.probe import probe_evidence


@pytest.mark.parametrize(("middle", "look"), [(87.0, "cartoon"), (86.999, "live_action")])
def test_ten_budgeted_scores_sort_at_the_inclusive_median(middle: float, look: str) -> None:
    scores = iter([21, 100, 35, middle - 1, 99, 42, middle + 1, 96, 40, 97])
    starts = []
    rates = []
    attempts = []
    policy = tomllib.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    runner = Mock(spec=RemoteMediaCommands)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = ""
        if "-ss" in command:
            starts.append(float(command[command.index("-ss") + 1]))
            attempts.clear()
        if "-b:v" in command:
            attempts.append(int(command[command.index("-b:v") + 1]))
            rates.append(attempts[-1])
        if "-show_packets" in command:
            size = 4096
            if command[-1].endswith("candidate.mkv"):
                size = round(attempts[0] / 8 * 2.5 / 60 * (2 if len(attempts) == 1 else 1))
            output = json.dumps({"packets": [{"pts_time": n / 24, "duration_time": 1 / 24, "size": size} for n in range(60)]})
        if command[0] == "cat":
            output = json.dumps({"frames": [{}] * 60, "pooled_metrics": {"vmaf": {"mean": next(scores)}}})
        return subprocess.CompletedProcess(command, 0, stdout=output)

    runner.run.side_effect = run
    stream = {
        "codec_type": "video",
        "width": 1920,
        "height": 1080,
        "field_order": "progressive",
        "avg_frame_rate": "24/1",
        "r_frame_rate": "24/1",
        "time_base": "1/1000",
    }
    payload = {"streams": [stream], "format": {"duration": 100, "size": 100_000_000}}
    result = probe_evidence(
        Path("source.mkv"),
        probe.ANIMATION_PROBE,
        probe_payload=payload,
        command_runner=runner,
        source_fingerprint="original",
        animation_policy=policy,
    )
    assert starts == pytest.approx([12 + 8 * i for i in range(10)])
    assert len(rates) == 2 * len(starts)
    assert rates[1] == pytest.approx(rates[0] / 2, abs=100)
    assert result["median_score"] == pytest.approx(middle)
    score = result["median_score"]
    assert isinstance(score, float)
    assert probe.animation_look(score) == look


@pytest.mark.parametrize(
    "metadata",
    [
        {"color_transfer": "arib-std-b67"},
        {"color_transfer": "smpte2084"},
        {"side_data_list": [{"side_data_type": "DOVI configuration record"}]},
        {"codec_tag_string": "dvh1"},
        {"side_data_list": [{"side_data_type": "Mastering display metadata"}]},
    ],
)
def test_hdr_bypasses_the_sdr_picture_measurement(metadata: dict[str, object]) -> None:
    streams = [{"codec_type": "video", "disposition": {"attached_pic": 1}}, {"codec_type": "video", **metadata}]
    assert probe_evidence(Path("source.mkv"), probe.ANIMATION_PROBE, probe_payload={"streams": streams}) == {"hdr": True}


def test_picture_allowance_counts_a_large_untagged_copied_track() -> None:
    policy = tomllib.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    item: dict[str, Any] = {
        "audio_summary": [{"index": 1, "codec_name": "aac", "channels": 2, "language": "eng", "size_bytes": None}],
        "subtitle_summary": [{"index": 2, "codec_name": "hdmv_pgs_subtitle", "language": "eng", "size_bytes": None}],
        "attachment_summary": [],
        "source_size_bytes": 200_000_000,
        "duration_seconds": 2700,
        "rel_path": "feature.mkv",
        "resolved_policy": policy,
    }
    read = Mock(return_value="0,9000000\n1,173000000\n2,150000\n")
    copied = probe._picture_budget(item, Path("feature.mkv"), "ffprobe", read)
    read.assert_called_once()
    assert item["subtitle_summary"][0]["size_bytes"] == 150_000
    item["audio_summary"] = [{"index": 1, "codec_name": "eac3", "channels": 2, "size_bytes": None}]
    stereo = probe._picture_budget(item, Path("feature.mkv"), "ffprobe", read)
    item["source_size_bytes"] = 120_000_000
    small_original = probe._picture_budget(item, Path("feature.mkv"), "ffprobe", read)
    read.assert_called_once()
    assert small_original == stereo
    assert copied.bytes_per_second > 0
    assert copied.bytes_per_second == stereo.bytes_per_second


def test_context_scores_only_the_scheduled_frames_within_a_measured_budget() -> None:
    from unittest.mock import patch

    check = probe.AnimationCheck(Mock(), "ffmpeg", "ffprobe", Path("/fixture"), {}, None, probe.PictureBudget(100, 90, 110))
    short = probe.PictureSample(60, 2.5, 400, False)
    prefix = probe.PictureSample(192, 8, 600, False)
    context = probe.PictureSample(252, 10.5, 1000, False)
    with (
        patch.object(probe.AnimationCheck, "reference", side_effect=[short, prefix]),
        patch.object(probe.AnimationCheck, "sample", return_value=context),
        patch.object(probe.AnimationCheck, "encode_at_budget", side_effect=[False, True]),
        patch.object(probe.AnimationCheck, "score", return_value=64) as score,
    ):
        assert check.moment(Path("feature.mkv"), 25, 2.5) == 64
    score.assert_called_once_with(check.scratch / "picture.mkv", check.scratch / "reference.mkv", short.frames, prefix.frames)
