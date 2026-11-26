import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mediaforce.library import animation_probe as probe
from mediaforce.library.probe import probe_evidence


@pytest.mark.parametrize(("rate", "flat", "expected"), [
    (100, 0.8, "cartoon"), (100, 0.25, "live_action"), (5000, 0.8, "live_action"),
])
def test_ten_windows_trim_rate_outliers_and_require_flat_pictures(rate: int, flat: float, expected: str) -> None:
    rates = iter([1, 2, *([rate] * 6), 10_000, 20_000])
    starts = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["nice", "-n", "19"]
        if "framecrc" in command:
            starts.append(float(command[command.index("-ss") + 1]))
            output = f"#tb 0: 1/1000\n0, 0, 0, 2500, {round(next(rates) * 312.5)}, 0"
        else:
            output = f"lavfi.signalstats.YAVG={flat * 255}"
        return subprocess.CompletedProcess(command, 0, stdout=output)

    with patch.object(probe, "run_command", side_effect=run), patch.object(probe, "ffmpeg_binary", return_value="ffmpeg"):
        result = probe.probe_animation(Path("source.mkv"), 100.0, video_stream={}, process_controller=None, command_runner=None)
    assert starts == pytest.approx([12 + 8 * i for i in range(10)])
    assert result == pytest.approx({"kbps": rate, "flat_fraction": flat})
    kbps, fraction = result["kbps"], result["flat_fraction"]
    assert isinstance(kbps, float) and isinstance(fraction, float)
    assert probe.animation_look(kbps, fraction) == expected


@pytest.mark.parametrize("metadata", [
    {"color_transfer": "arib-std-b67"}, {"color_transfer": "smpte2084"},
    {"side_data_list": [{"side_data_type": "DOVI configuration record"}]}, {"codec_tag_string": "dvh1"},
    {"side_data_list": [{"side_data_type": "Mastering display metadata"}]},
])
def test_hdr_bypasses_the_sdr_picture_measurement(metadata: dict[str, object]) -> None:
    streams = [{"codec_type": "video", "disposition": {"attached_pic": 1}}, {"codec_type": "video", **metadata}]
    assert probe_evidence(Path("source.mkv"), probe.ANIMATION_PROBE, probe_payload={"streams": streams}) == {"hdr": True}


def test_incomplete_picture_cannot_supply_a_sort() -> None:
    with pytest.raises(ValueError, match="did not finish"):
        probe._video_kbps("#tb 0: 1/1000\n0, 0, 0, 500, 1200, 0", 2.5)
