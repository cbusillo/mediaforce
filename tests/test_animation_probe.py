import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mediaforce.encoding.streams import resolve_stream_plan
from mediaforce.library import animation_probe


def _fingerprint() -> dict[str, object]:
    moments = [{"start_seconds": start, "seconds": 10.0, "motion": motion, "detail": detail, "texture": detail}
               for start, motion, detail in [(0, 1, 1), (20, 6, 2), (40, 2, 8), (60, 3, 5)]]
    return {"analysis": {"ranges": [{"status": "measured", "picture_moments": moments}]}}


@pytest.mark.parametrize(("scores", "expected"), [
    ([90, 84, 88], "cartoon"), ([90, 75, 88], "live_action"), ([90, 80.5, 88], "unmeasured"),
])
def test_worst_hard_scene_decides_and_only_a_close_call_keeps_that_pair(tmp_path: Path, scores: list[float], expected: str) -> None:
    starts = []
    scored = iter(scores)
    source = tmp_path / "episode.mkv"
    plan = resolve_stream_plan({"audio_summary": [{"index": 2, "codec_name": "aac"}], "attachment_summary": []})
    review = tmp_path / "review"
    target = animation_probe.CARTOON_TARGET_MB * 1_000_000 * animation_probe.PICTURE_MOMENT_SECONDS / animation_probe.REFERENCE_RUNTIME_SECONDS

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "ab-av1" in command:
            stdout = str(next(scored))
        elif "-progress" in command:
            size = 900_000
            if "ffv1" in command:
                starts.append(float(command[command.index("-ss") + 1]))
            elif "libsvtav1" in command:
                size = round(target - 100_000)
                assert abs(int(command[command.index("-b:v") + 1]) - size * 8 / animation_probe.PICTURE_MOMENT_SECONDS) < 1
            stdout = f"frame=240\ntotal_size={size}\nout_time_us=10000000\nprogress=end"
        else:
            Path(command[-1]).write_bytes(b"comparison")
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    with patch.object(animation_probe, "run_command", side_effect=run), patch.object(animation_probe, "run_local_quality_command", side_effect=run), patch.object(animation_probe, "ffmpeg_binary", return_value="ffmpeg"):
        result = animation_probe.probe_animation(source, 90, review_dir=review, fingerprint=_fingerprint(), plan=plan,
            video_policy={"min_target_vmaf": 80, "preset": 4}, process_controller=None, command_runner=None)
    assert starts == [20, 40, 60]
    assert result["picture_look"] == expected
    if expected == "unmeasured":
        moment = result["moment"]
        assert moment["at_seconds"] == 40
        for side in ("original", "sample"):
            assert (review / moment[side]["url"].removeprefix("/review-media/")).is_file()
    else:
        assert list(review.iterdir()) == []


def test_size_miss_never_receives_a_picture_verdict(tmp_path: Path) -> None:
    def oversize(command: list[str]) -> str:
        assert "ab-av1" not in command
        return "frame=240\ntotal_size=2000000\nprogress=end"

    with pytest.raises(ValueError, match="cartoon size"):
        animation_probe._picture_sample(oversize, ["ffmpeg"], tmp_path / "ref", tmp_path / "out", {"preset": 4}, 500_000, 100_000, 10, 240)
