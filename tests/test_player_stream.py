import io
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import av
import pytest

from mediaforce.web.runtime import player_stream

FRAME_RATE = 24
SAMPLE_RATE = 48000
SOUND_FRAME = 1024


def _episode_file(path: Path, seconds: int, speakers: str | None = None) -> Path:
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=FRAME_RATE)
        stream.width = stream.height = 64
        stream.pix_fmt = "yuv420p"
        stream.codec_context.gop_size = FRAME_RATE // 2
        sound = container.add_stream("aac", rate=SAMPLE_RATE, layout=speakers) if speakers else None
        for index in range(seconds * FRAME_RATE):
            frame = av.VideoFrame(64, 64, "yuv420p")
            frame.pts = index
            frame.time_base = Fraction(1, FRAME_RATE)
            container.mux(stream.encode(frame))
        container.mux(stream.encode())
        if sound is not None:
            for start in range(0, seconds * SAMPLE_RATE, SOUND_FRAME):
                heard = av.AudioFrame(format="fltp", layout=sound.layout, samples=SOUND_FRAME)
                heard.sample_rate, heard.pts = SAMPLE_RATE, start
                for plane in heard.planes:
                    plane.update(bytes(plane.buffer_size))
                container.mux(sound.encode(heard))
            container.mux(sound.encode())
    return path


def _played(source: Path) -> list[bytes]:
    """Each piece as the player reads it: the playlist's header followed by the piece."""
    lines = player_stream.serve(source, "index.m3u8").body.decode().splitlines()
    header = next(line.split('"')[1] for line in lines if line.startswith("#EXT-X-MAP:"))
    init = player_stream.serve(source, header).body
    return [init + player_stream.serve(source, line).body for line in lines if line and not line.startswith("#")]


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

    streamed: list[float] = []
    for cut, piece in zip(facts.cuts, _played(source), strict=True):
        segment = _picture(piece)
        assert segment[0] == (round(cut, 3), True)
        streamed += [time for time, _ in segment]

    assert len(facts.cuts) == 4
    assert sorted(streamed) == sorted(time for time, _ in _picture(source.read_bytes()))


def test_an_episode_without_sound_streams_its_picture_alone(tmp_path: Path) -> None:
    source = _episode_file(tmp_path / "episode.mkv", seconds=8)

    pieces = _played(source)

    assert len(pieces) == len(player_stream.episode(source).cuts)
    for piece in pieces:
        with av.open(io.BytesIO(piece), "r") as container:
            assert [stream.type for stream in container.streams] == ["video"]
    for missing in (f"{len(pieces)}.m4s", "sound.m3u8", "picture.mp4"):
        with pytest.raises(player_stream.SegmentNotFound):
            player_stream.serve(source, missing)


@pytest.mark.parametrize(("speakers", "copied"), [("5.1", True), ("5.1(side)", False)])
def test_each_piece_carries_picture_and_sound_the_mac_plays(tmp_path: Path, speakers: str, copied: bool) -> None:
    # A list of variants with sound as its own rendition fails on a Mac without an HDR screen when the picture is HDR,
    # and the Mac cannot decode AAC whose speakers are named in its own table (5.1 side), so it is decoded instead.
    source = _episode_file(tmp_path / "episode.mkv", seconds=8, speakers=speakers)

    for piece in _played(source):
        with av.open(io.BytesIO(piece), "r") as container:
            assert [stream.type for stream in container.streams] == ["video", "audio"]
            assert (container.streams.audio[0].codec_context.name == "aac") is copied
            assert any(packet.size for packet in container.demux(container.streams.audio[0]))


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
        command = player_stream._sound_command(
            Path("episode.mkv"), sound, 30.0, 36.0, copy=player_stream._player_plays(sound, b"")
        )

    assert (command[command.index("-c:a") + 1] == "copy") is copied
    assert float(command[command.index("-ss") + 1]) == 30.0 - player_stream.SOUND_LEAD_SECONDS
    assert float(command[command.index("-output_ts_offset") + 1]) == 30.0
