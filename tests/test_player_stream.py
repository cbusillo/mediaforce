import io
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import av
import pytest

from mediaforce.web.runtime import player_stream

FRAME_RATE = 24


def _episode_file(path: Path, seconds: int) -> Path:
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=FRAME_RATE)
        stream.width = stream.height = 64
        stream.pix_fmt = "yuv420p"
        stream.codec_context.gop_size = FRAME_RATE // 2
        for index in range(seconds * FRAME_RATE):
            frame = av.VideoFrame(64, 64, "yuv420p")
            frame.pts = index
            frame.time_base = Fraction(1, FRAME_RATE)
            container.mux(stream.encode(frame))
        container.mux(stream.encode())
    return path


def _picture(data: bytes) -> list[tuple[float, bool]]:
    with av.open(io.BytesIO(data), "r") as container:
        video = container.streams.video[0]
        scale = float(video.time_base or 0)
        packets: list[tuple[float, bool]] = []
        for packet in container.demux(video):
            if packet.size and packet.pts is not None:
                packets.append((round(packet.pts * scale, 3), packet.is_keyframe))
        return packets


def test_picture_segments_cover_the_episode_once_from_keyframe_to_keyframe(tmp_path: Path) -> None:
    source = _episode_file(tmp_path / "episode.mkv", seconds=20)
    facts = player_stream.episode(source)
    header = player_stream.serve(source, "picture-init.mp4").body

    streamed: list[float] = []
    for index, cut in enumerate(facts.cuts):
        segment = _picture(header + player_stream.serve(source, f"picture-{index}.m4s").body)
        assert segment[0] == (round(cut, 3), True)
        streamed += [time for time, _ in segment]

    assert len(facts.cuts) == 4
    assert sorted(streamed) == sorted(time for time, _ in _picture(source.read_bytes()))


def test_an_episode_without_sound_streams_its_picture_alone(tmp_path: Path) -> None:
    source = _episode_file(tmp_path / "episode.mkv", seconds=8)

    main = player_stream.serve(source, "index.m3u8").body.decode()
    picture = player_stream.serve(source, "picture.m3u8").body.decode()

    assert "picture.m3u8" in main and "sound" not in main
    assert picture.count(".m4s") == len(player_stream.episode(source).cuts)
    for missing in ("sound.m3u8", "picture-9.m4s", "picture.mp4"):
        with pytest.raises(player_stream.SegmentNotFound):
            player_stream.serve(source, missing)


def test_packets_read_after_a_seek_get_decode_times_one_frame_apart() -> None:
    packets = [av.Packet(b"frame") for _ in range(3)]
    for packet, decode_time in zip(packets, (None, None, 10), strict=True):
        packet.dts = decode_time
        packet.duration = 1

    assert [packet.dts for packet in player_stream._with_decode_times(packets)] == [8, 9, 10]


def test_segments_start_at_a_keyframe_at_least_six_seconds_after_the_last() -> None:
    assert player_stream.cuts([0.0, 2.5, 5.0, 7.5, 10.0, 12.5, 15.0, 16.0]) == (0.0, 7.5, 15.0)


@pytest.mark.parametrize(("sound", "copied"), [("opus", True), ("eac3", True), ("dts", False), ("truehd", False)])
def test_sound_is_copied_when_the_mac_plays_it_and_read_from_before_the_cut(sound: str, copied: bool) -> None:
    with patch.object(player_stream, "ffmpeg_binary", return_value="ffmpeg"):
        command = player_stream._sound_command(Path("episode.mkv"), sound, 30.0, 36.0)

    assert (command[command.index("-c:a") + 1] == "copy") is copied
    assert float(command[command.index("-ss") + 1]) == 30.0 - player_stream.SOUND_LEAD_SECONDS
    assert float(command[command.index("-output_ts_offset") + 1]) == 30.0
