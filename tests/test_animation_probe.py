import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mediaforce.library import animation_probe


@pytest.mark.parametrize(("rates", "expected"), [
    ([380, 530, 950], "cartoon"), ([620, 1040, 1225], "live_action"),
    ([610, animation_probe.CUTOFF_KBPS, 980], "unmeasured"),
])
def test_three_clips_choose_a_look_or_keep_a_comparison(tmp_path: Path, rates: list[int], expected: str) -> None:
    encoded = []
    source = tmp_path / "episode.mkv"
    source.touch()

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["nice", "-n", "19"]
        if "-progress" in command:
            rate = rates[len(encoded)]
            encoded.append(command)
            Path(command[-1]).touch()
            stdout = f"frame=240\ntotal_size={rate * 125 * animation_probe.CLIP_SECONDS}\nout_time_us=10000000\nprogress=end"
        else:
            Path(command[-1]).write_bytes(b"playable fixture")
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    with patch.object(animation_probe, "run_command", side_effect=run), patch.object(animation_probe, "ffmpeg_binary", return_value="ffmpeg"):
        result = animation_probe.probe_animation(source, 90, review_dir=tmp_path / "review", process_controller=None, command_runner=None)
    assert len({command[command.index("-ss") + 1] for command in encoded}) == 3
    bitrate = result["kbps"]
    assert isinstance(bitrate, float) and animation_probe.animation_look(bitrate) == expected
    if expected == "unmeasured":
        moment = result["moment"]
        assert isinstance(moment, dict)
        for side in ("original", "sample"):
            assert (tmp_path / "review" / moment[side]["url"].removeprefix("/review-media/")).is_file()
    else:
        assert list((tmp_path / "review").iterdir()) == []
